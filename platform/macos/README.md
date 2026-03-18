# macOS Setup

## Prerequisites

- Node.js 18+
- MongoDB Atlas cluster (or local MongoDB)

## Installation

```bash
# Clone the repo
git clone https://github.com/dhslim/claude-toolkit.git
cd claude-toolkit

# Run setup
bash platform/macos/setup.sh
```

The setup script will:
1. Install npm dependencies
2. Create `.env` with your MongoDB URI
3. Print the hook configuration to add to `~/.claude/settings.json`

## Manual hook setup

Copy the hooks JSON from the setup output into `~/.claude/settings.json`. The hooks use absolute paths to the cloned repo location.

## Verify

```bash
node sync-conversations.js --scan
```

## Notes

- Conversation JSONL files are read from `~/.claude/projects/`
- Sync logs are written to `sync.log` in the repo directory
- The `windowsHide` option in `hook-sync.js` is harmless on macOS (ignored by Node.js)
