# Register a per-user Scheduled Task that starts the hub at logon.
# Remove with: Unregister-ScheduledTask -TaskName InfoHub -Confirm:$false
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -File `"$root\scripts\start.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT2M'   # give sources time to start and sync
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName InfoHub -Action $action -Trigger $trigger -Settings $settings -Description 'Personal information hub' -Force
