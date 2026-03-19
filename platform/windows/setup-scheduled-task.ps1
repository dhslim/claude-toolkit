# conversation-warehouse hourly sync scheduled task setup
# Run as admin: powershell -ExecutionPolicy Bypass -File platform\windows\setup-scheduled-task.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))

$taskName = "ConversationWarehouseSync"
$syncScript = Join-Path $ScriptDir "sync_conversations.py"
$pythonPath = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonPath)) {
    Write-Error "Python venv not found at $pythonPath. Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

# Remove existing task
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Hourly trigger
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 365)

# Action: python sync_conversations.py --scan
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "$syncScript --scan" -WorkingDirectory $ScriptDir

# Settings: run even on battery, start when available
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# Register
Register-ScheduledTask -TaskName $taskName -Trigger $trigger -Action $action -Settings $settings -Description "Sync Claude Code conversations to MongoDB Atlas hourly" -RunLevel Limited

Write-Host "Scheduled task '$taskName' registered (runs hourly)"
Write-Host "Verify: Get-ScheduledTaskInfo -TaskName '$taskName'"
