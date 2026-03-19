# conversation-warehouse Windows setup — run this after cloning
# Usage: powershell -ExecutionPolicy Bypass -File platform\windows\setup.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))

Write-Host "=== conversation-warehouse setup (Windows) ==="

# 1. Create venv and install dependencies
$VenvDir = Join-Path $ScriptDir ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
$PythonW = Join-Path $VenvDir "Scripts\pythonw.exe"

if (-not (Test-Path $Python)) {
    Write-Host "Creating Python venv..."
    python -m venv $VenvDir
}

Write-Host "Installing dependencies..."
& (Join-Path $VenvDir "Scripts\pip.exe") install -r (Join-Path $ScriptDir "requirements.txt")

# 2. Create .env if not exists
$envFile = Join-Path $ScriptDir ".env"
if (-not (Test-Path $envFile)) {
    $mongoUri = Read-Host "MongoDB URI"
    "MONGODB_URI=$mongoUri" | Out-File -FilePath $envFile -Encoding utf8
    Write-Host ".env created"
} else {
    Write-Host ".env already exists, skipping"
}

# 3. Show hook configuration
$settingsDir = Join-Path $env:USERPROFILE ".claude"
$settingsFile = Join-Path $settingsDir "settings.json"

if (-not (Test-Path $settingsDir)) {
    New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null
}

# Use forward slashes for Claude Code compatibility
$SD = $ScriptDir -replace '\\', '/'
$PW = $PythonW -replace '\\', '/'
$PY = $Python -replace '\\', '/'

if (Test-Path $settingsFile) {
    Write-Host ""
    Write-Host "Existing settings.json found."
    Write-Host "Merge the hooks section below into your settings:"
} else {
    Write-Host ""
    Write-Host "No settings.json found. Create one with the hooks below:"
}

Write-Host @"

=== Add this to ~/.claude/settings.json hooks section ===

{
  "hooks": {
    "Stop": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "$PW $SD/hook_sync.py",
        "async": true, "timeout": 30000 },
      { "type": "command",
        "command": "$PY $SD/quiz_check.py",
        "timeout": 3000 }
    ]}],
    "SessionEnd": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "$PW $SD/hook_sync.py",
      "timeout": 10000
    }]}],
    "SessionStart": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "$PW $SD/sync_conversations.py --scan",
        "async": true, "timeout": 60000 },
      { "type": "command",
        "command": "$PY $SD/quiz_check.py",
        "timeout": 3000 }
    ]}]
  }
}
"@

Write-Host ""
Write-Host "=== Setup complete ==="
Write-Host "Test: $PY $SD/sync_conversations.py --scan"
