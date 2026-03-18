# claude-toolkit Windows setup — run this after cloning
# Usage: powershell -ExecutionPolicy Bypass -File platform\windows\setup.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))

Write-Host "=== claude-toolkit setup (Windows) ==="

# 1. Install dependencies
Write-Host "Installing npm dependencies..."
Push-Location $ScriptDir
npm install
Pop-Location

# 2. Create .env if not exists
$envFile = Join-Path $ScriptDir ".env"
if (-not (Test-Path $envFile)) {
    $mongoUri = Read-Host "MongoDB URI"
    @"
MONGODB_URI=$mongoUri
"@ | Out-File -FilePath $envFile -Encoding utf8
    Write-Host ".env created"
} else {
    Write-Host ".env already exists, skipping"
}

# 3. Show hook configuration
$settingsFile = Join-Path $env:USERPROFILE ".claude\settings.json"

if (Test-Path $settingsFile) {
    Write-Host ""
    Write-Host "Existing settings.json found."
    Write-Host "You'll need to manually add the hooks section."
    Write-Host "See below for the hook configuration:"
} else {
    Write-Host ""
    Write-Host "No settings.json found."
    New-Item -ItemType Directory -Path (Join-Path $env:USERPROFILE ".claude") -Force | Out-Null
    Write-Host "See below for the hook configuration to add:"
}

$escapedDir = $ScriptDir -replace '\\', '/'

Write-Host @"

=== Add this to ~/.claude/settings.json hooks section ===

{
  "hooks": {
    "Stop": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node $escapedDir/hook-sync.js",
      "async": true, "timeout": 30000
    }]}],
    "SessionEnd": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node $escapedDir/hook-sync.js",
      "timeout": 10000
    }]}],
    "SessionStart": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "node $escapedDir/sync-conversations.js --scan",
        "async": true, "timeout": 60000 },
      { "type": "command",
        "command": "node $escapedDir/quiz-check.js",
        "timeout": 3000 }
    ]}]
  }
}
"@

Write-Host "=== Setup complete ==="
Write-Host "Test: node $escapedDir/sync-conversations.js --scan"
