# Install Prompt

Copy the prompt below and paste it into Claude Code. Claude Code will handle the entire setup — venv, dependencies, .env, and hooks — on any OS.

---

## Prompt

```
Install the conversation-warehouse toolkit from the repo at REPO_DIR (replace with the actual path to the cloned claude-toolkit repo).

Follow these steps exactly:

### 1. Python venv and dependencies

Create a Python virtual environment in the repo directory and install dependencies:

- Linux/macOS:
  python3 -m venv REPO_DIR/.venv
  REPO_DIR/.venv/bin/pip install -r REPO_DIR/requirements.txt

- Windows:
  python -m venv REPO_DIR\.venv
  REPO_DIR\.venv\Scripts\pip install -r REPO_DIR\requirements.txt

### 2. Create .env

If REPO_DIR/.env does not already exist, ask me for my MongoDB URI, then create REPO_DIR/.env with:

  MONGODB_URI=<the URI I provide>

If .env already exists, skip this step.

### 3. Test the connection

Run the sync script in scan mode to verify MongoDB connectivity:

- Linux/macOS: REPO_DIR/.venv/bin/python REPO_DIR/sync_conversations.py --scan
- Windows: REPO_DIR\.venv\Scripts\python REPO_DIR\sync_conversations.py --scan

If it fails with a TLS/SSL error, the Python or OpenSSL version may be too old. Upgrade Python to 3.10+ and retry.

### 4. Add hooks to ~/.claude/settings.json

Read the existing ~/.claude/settings.json (create it if it doesn't exist). Merge the following hooks into the "hooks" key, preserving any existing hooks that are already there.

Use PYTHON as the absolute path to the venv python:
- Linux/macOS: REPO_DIR/.venv/bin/python
- Windows: REPO_DIR/.venv/Scripts/pythonw.exe (for async hooks) and REPO_DIR/.venv/Scripts/python.exe (for sync hooks like quiz_check)

Hooks to add:

Stop:
  1. PYTHON REPO_DIR/hook_sync.py — async, timeout 30000ms
  2. PYTHON REPO_DIR/quiz_check.py — timeout 3000ms
     (On Windows, use python.exe not pythonw.exe for this one)

SessionEnd:
  1. PYTHON REPO_DIR/hook_sync.py — timeout 10000ms

SessionStart:
  1. PYTHON REPO_DIR/sync_conversations.py --scan — async, timeout 60000ms
  2. PYTHON REPO_DIR/quiz_check.py — timeout 3000ms
     (On Windows, use python.exe not pythonw.exe for this one)

The hooks JSON structure for each entry is:
{
  "matcher": "",
  "hooks": [{
    "type": "command",
    "command": "<full command string>",
    "async": true/false,
    "timeout": <ms>
  }]
}

Only include "async": true when specified above. Omit it otherwise.

### 5. Verify

Run the sync scan one more time to confirm everything works:
- Linux/macOS: REPO_DIR/.venv/bin/python REPO_DIR/sync_conversations.py --scan
- Windows: REPO_DIR\.venv\Scripts\python REPO_DIR\sync_conversations.py --scan

Tell me the result (how many files found, inserted, updated, skipped, errors).
```

---

## Usage

1. Clone the repo: `git clone https://github.com/dhslim/claude-toolkit.git`
2. Open Claude Code in any directory
3. Paste the prompt above, replacing `REPO_DIR` with the path to the cloned repo (e.g. `~/claude-toolkit` or `C:\Users\you\claude-toolkit`)
4. Claude Code handles everything
