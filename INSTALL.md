# Install Prompt

Clone the repo, `cd` into it, open Claude Code, and paste the prompt below. Claude Code will handle the entire setup — venv, dependencies, .env, and hooks — on any OS.

---

## Prompt

```
Install the conversation-warehouse toolkit from the current working directory.

Use absolute paths for all commands and hook configurations.

Follow these steps exactly:

### 1. Python venv and dependencies

Create a Python virtual environment in this directory and install dependencies from requirements.txt.

### 2. Create .env

If .env does not already exist in this directory, ask me for my MongoDB URI, then create .env with:

  MONGODB_URI=<the URI I provide>

If .env already exists, skip this step.

### 3. Test the connection

Run sync_conversations.py --scan using the venv python to verify MongoDB connectivity.

If it fails with a TLS/SSL error, the Python or OpenSSL version may be too old. Upgrade Python to 3.10+ and retry.

### 4. Add hooks to ~/.claude/settings.json

Read the existing ~/.claude/settings.json (create it if it doesn't exist). Merge the following hooks into the "hooks" key, preserving any existing hooks that are already there.

For hook commands, use the absolute path to the venv python and the absolute path to each script.

On Windows there are two python executables in the venv:
- pythonw.exe — runs without opening a console window (use for background/async hooks)
- python.exe — runs with stdout visible (use when Claude needs to read the hook's output)

Hooks to add:

Stop:
  1. hook_sync.py — async, timeout 30000ms (Windows: pythonw.exe)
  2. quiz_check.py — timeout 5000ms (Windows: python.exe)

SessionEnd:
  1. hook_sync.py — async, timeout 10000ms (Windows: pythonw.exe)
  2. hook_session_guard_cleanup.py — async, timeout 5000ms (Windows: pythonw.exe)

SessionStart:
  1. hook_session_guard.py — timeout 5000ms (Windows: python.exe)
  2. sync_conversations.py --scan — async, timeout 60000ms (Windows: pythonw.exe)

The hooks JSON structure for each entry is:
{
  "matcher": "",
  "hooks": [{
    "type": "command",
    "command": "<full command string>",
    "async": true,
    "timeout": <ms>
  }]
}

Only include "async": true when specified above. Omit it otherwise.

### 5. Update global CLAUDE.md

Read ~/.claude/CLAUDE.md (create it if it doesn't exist). If there is no "MongoDB Atlas" section, append the following block. If there is one, update it to match. Preserve all other existing content.

Use the absolute path to this repo's directory and the venv python path (platform-appropriate).

```
## MongoDB Atlas
- When referencing "MongoDB", this means the MongoDB Atlas cluster.
- Scripts: `<absolute path to this repo>` (Python, venv at `.venv/`)
- Connection: uses `MONGODB_URI` from `<absolute path to this repo>/.env`
- Database: `conversation-warehouse`
- To query: `cd` to the scripts directory and use the venv python with `from _shared import get_db`.
- Schema and collections are documented in the repo's README.md.
```

### 6. Add shell aliases

Append the following to ~/.zshrc (or ~/.bashrc on Linux), if not already present:

```bash
# Claude Code fork shortcuts
alias cfork='claude -r --fork-session'   # pick a session to fork-resume
alias cread='claude -c --fork-session'   # fork-continue latest session (for reading)
```

### 7. Verify

Run sync_conversations.py --scan one more time to confirm everything works.

Tell me the result (how many files found, inserted, updated, skipped, errors).

Remind me to restart Claude Code so the hooks are guaranteed to load.
```

---

## Usage

1. Clone the repo: `git clone https://github.com/dhslim/claude-toolkit.git`
2. `cd claude-toolkit`
3. Open Claude Code
4. Paste the prompt above as-is — no edits needed
