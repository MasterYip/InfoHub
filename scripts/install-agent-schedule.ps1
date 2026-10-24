# Register a per-user Scheduled Task that runs scripts/agent-run.ps1 every N hours.
# Remove with: Unregister-ScheduledTask -TaskName InfoHubAgent -Confirm:$false
param([int]$Hours = 2)
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -File `"$root\scripts\agent-run.ps1`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours $Hours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName InfoHubAgent -Action $action -Trigger $trigger -Settings $settings -Description 'info-hub scheduled agent run' -Force
