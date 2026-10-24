# Run the configured external agent once (Codex, Claude Code, ...) with agents/scheduled-run.md.
# Configure in config/hub.local.yaml:  agent: {command: "codex exec --full-auto"}
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
$py = "$root\.venv\Scripts\python.exe"
$cmd = & $py -c "from infohub import config; c=config.load(); print(c['agent']['command'])"
if (-not $cmd) { Write-Host "agent.command is empty in config/hub.local.yaml"; exit 1 }
$prompt = Get-Content -Raw "$root\agents\scheduled-run.md"
$log = "$root\logs\agent-$(Get-Date -Format yyyyMMdd-HHmm).log"
$parts = $cmd -split ' '
& $parts[0] @($parts[1..($parts.Length-1)] + $prompt) *>&1 | Tee-Object -FilePath $log
