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
  2. quiz_check.py — timeout 3000ms (Windows: python.exe)

SessionEnd:
  1. hook_sync.py — timeout 10000ms (Windows: pythonw.exe)

SessionStart:
  1. sync_conversations.py --scan — async, timeout 60000ms (Windows: pythonw.exe)
  2. quiz_check.py — timeout 3000ms (Windows: python.exe)

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

### 5. Verify

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
