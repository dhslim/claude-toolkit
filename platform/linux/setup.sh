#!/bin/bash
# claude-toolkit Linux setup — run this after cloning
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "=== claude-toolkit setup (Linux) ==="

# 1. Install dependencies
echo "Installing npm dependencies..."
cd "$SCRIPT_DIR"
npm install

# 2. Create .env if not exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo ""
  read -p "MongoDB URI: " MONGO_URI
  echo "MONGODB_URI=$MONGO_URI" > "$SCRIPT_DIR/.env"
  echo ".env created"
else
  echo ".env already exists, skipping"
fi

# 3. Patch ~/.claude/settings.json with hooks
SETTINGS_FILE="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"

if [ -f "$SETTINGS_FILE" ]; then
  echo ""
  echo "Existing settings.json found."
  echo "You'll need to manually add the hooks section."
  echo "See below for the hook configuration:"
else
  echo ""
  echo "No settings.json found. Creating one with hooks..."
fi

# Print hook config for user to verify/apply
cat << HOOKEOF

=== Add this to ~/.claude/settings.json hooks section ===

{
  "hooks": {
    "Stop": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node $SCRIPT_DIR/hook-sync.js",
      "async": true, "timeout": 30000
    }]}],
    "SessionEnd": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node $SCRIPT_DIR/hook-sync.js",
      "timeout": 10000
    }]}],
    "SessionStart": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "node $SCRIPT_DIR/sync-conversations.js --scan",
        "async": true, "timeout": 60000 },
      { "type": "command",
        "command": "node $SCRIPT_DIR/quiz-check.js",
        "timeout": 3000 }
    ]}]
  }
}

HOOKEOF

echo "=== Setup complete ==="
echo "Test: node $SCRIPT_DIR/sync-conversations.js --scan"
