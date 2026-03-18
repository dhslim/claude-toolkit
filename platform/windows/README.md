# Windows Setup

## Prerequisites

- Node.js 18+
- MongoDB Atlas cluster (or local MongoDB)

## Installation

```powershell
# Clone the repo
git clone https://github.com/dhslim/claude-toolkit.git
cd claude-toolkit

# Run setup
powershell -ExecutionPolicy Bypass -File platform\windows\setup.ps1
```

The setup script will:
1. Install npm dependencies
2. Create `.env` with your MongoDB URI
3. Print the hook configuration to add to `~/.claude/settings.json`

## Optional: Hourly scheduled task

To also sync on a schedule (in addition to hooks):

```powershell
# Run as administrator
powershell -ExecutionPolicy Bypass -File platform\windows\setup-scheduled-task.ps1
```

This creates a Windows Scheduled Task that runs `sync-conversations.js --scan` every hour.

## Manual hook setup

Copy the hooks JSON from the setup output into `%USERPROFILE%\.claude\settings.json`. The hooks use absolute paths to the cloned repo location.

## Verify

```powershell
node sync-conversations.js --scan
```

## Notes

- Conversation JSONL files are read from `%USERPROFILE%\.claude\projects\`
- Sync logs are written to `sync.log` in the repo directory
- `hook-sync.js` uses `windowsHide: true` to prevent console windows from flashing
