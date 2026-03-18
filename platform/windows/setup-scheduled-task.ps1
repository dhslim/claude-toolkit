# conversation-warehouse hourly sync scheduled task setup
# Run as admin: powershell -ExecutionPolicy Bypass -File platform\windows\setup-scheduled-task.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))

$taskName = "ConversationWarehouseSync"
$scriptPath = Join-Path $ScriptDir "sync-conversations.js"
$nodePath = (Get-Command node).Source

# Remove existing task
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Hourly trigger
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 365)

# Action: node sync-conversations.js
$action = New-ScheduledTaskAction -Execute $nodePath -Argument $scriptPath -WorkingDirectory $ScriptDir

# Settings: run even on battery, start when available
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# Register
Register-ScheduledTask -TaskName $taskName -Trigger $trigger -Action $action -Settings $settings -Description "Sync Claude Code conversations to MongoDB Atlas hourly" -RunLevel Limited

Write-Host "Scheduled task '$taskName' registered (runs hourly)"
Write-Host "Verify: Get-ScheduledTaskInfo -TaskName '$taskName'"
