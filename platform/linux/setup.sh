#!/bin/bash
# conversation-warehouse Linux setup — run this after cloning
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "=== conversation-warehouse setup (Linux) ==="

# 1. Create venv and install dependencies
echo "Setting up Python venv..."
cd "$SCRIPT_DIR"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

PYTHON="$SCRIPT_DIR/.venv/bin/python"

# 2. Create .env if not exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo ""
  read -p "MongoDB URI: " MONGO_URI
  echo "MONGODB_URI=$MONGO_URI" > "$SCRIPT_DIR/.env"
  echo ".env created"
else
  echo ".env already exists, skipping"
fi

# 3. Install hooks into ~/.claude/settings.json
SETTINGS_FILE="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"

if [ -f "$SETTINGS_FILE" ]; then
  echo ""
  echo "Existing settings.json found."
  echo "You'll need to manually merge the hooks section below."
  echo "See below for the hook configuration:"
else
  echo ""
  echo "No settings.json found. Creating one with hooks..."
fi

cat << HOOKEOF

=== Add this to ~/.claude/settings.json hooks section ===

{
  "hooks": {
    "Stop": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "$PYTHON $SCRIPT_DIR/hook_sync.py",
        "async": true, "timeout": 30000 },
      { "type": "command",
        "command": "$PYTHON $SCRIPT_DIR/quiz_check.py",
        "timeout": 3000 }
    ]}],
    "SessionEnd": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "$PYTHON $SCRIPT_DIR/hook_sync.py",
      "timeout": 10000
    }]}],
    "SessionStart": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "$PYTHON $SCRIPT_DIR/sync_conversations.py --scan",
        "async": true, "timeout": 60000 },
      { "type": "command",
        "command": "$PYTHON $SCRIPT_DIR/quiz_check.py",
        "timeout": 3000 }
    ]}]
  }
}

HOOKEOF

echo "=== Setup complete ==="
echo "Test: $PYTHON $SCRIPT_DIR/sync_conversations.py --scan"
