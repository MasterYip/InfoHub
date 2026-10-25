# Register a per-user Scheduled Task that starts the hub at logon.
# Remove with: Unregister-ScheduledTask -TaskName InfoHub -Confirm:$false
param([switch]$Desktop)  # -Desktop starts a tray icon in your interactive logon session
$root = Split-Path -Parent $PSScriptRoot
$launcher = if ($Desktop) { "start-desktop.ps1" } else { "start.ps1" }
$extra = if ($Desktop) { " -Hidden" } else { "" }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -File `"$root\scripts\$launcher`"$extra"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT2M'   # give sources time to start and sync
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName InfoHub -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Personal information hub' -Force
