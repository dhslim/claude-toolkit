# Install Prompt

Clone the repo, `cd` into it, open Claude Code, and paste the prompt below. Claude Code will handle the entire setup — venv, dependencies, .env, and hooks — on any OS.

---

## Prompt

```
Install the claude-toolkit from the current working directory.

Use absolute paths for all commands and hook configurations.

Follow these steps exactly:

### 1. Install jq (required for status line)

The status line script requires `jq` to parse JSON. Check if it's installed (`jq --version`). If not, install it:

- **Windows:** `winget install jqlang.jq`
- **macOS:** `brew install jq`
- **Linux:** `sudo apt install jq` (or equivalent for your distro)

### 2. Python venv and dependencies

Create a Python virtual environment in this directory and install dependencies from requirements.txt.

### 3. Create .env

If .env does not already exist in this directory, ask me for my MongoDB URI, then create .env with:

  MONGODB_URI=<the URI I provide>

If .env already exists, skip this step.

### 4. Test the connection

Run sync_conversations.py --scan using the venv python to verify MongoDB connectivity.

If it fails with a TLS/SSL error, the Python or OpenSSL version may be too old. Upgrade Python to 3.10+ and retry.

### 5. Configure ~/.claude/settings.json

Read `settings.json` from this repo as a reference. It contains the complete structure including hooks, permissions, status line config, and other settings.

Create or update `~/.claude/settings.json` to match the structure in the reference file, but **replace all hardcoded paths** with absolute paths appropriate for the current machine:
- Replace all python/pythonw paths with the absolute path to the venv python in this repo
- Replace all script paths with the absolute path to each script in this repo

On Windows there are two python executables in the venv:
- pythonw.exe — runs without opening a console window (use for background/async hooks)
- python.exe — runs with stdout visible (use when Claude needs to read the hook's output)

On macOS/Linux use the single `python` executable in `.venv/bin/`.

Preserve any existing settings in ~/.claude/settings.json that are not in the reference file.

### 6. Install status line

Copy `statusline.sh` from this repo to `~/.claude/statusline.sh` and make it executable (`chmod +x`).

The settings.json already references this path via `~/.claude/statusline.sh`.

### 7. Update global CLAUDE.md

Read ~/.claude/CLAUDE.md (create it if it doesn't exist). If there is no "MongoDB Atlas" section, append the following block. If there is one, update it to match. Preserve all other existing content.

Use the absolute path to this repo's directory and the venv python path (platform-appropriate).

```
## MongoDB Atlas
- When referencing "MongoDB", this means the MongoDB Atlas cluster.
- Scripts: `<absolute path to this repo>` (Python, venv at `.venv/`)
- Connection: uses `MONGODB_URI` from `<absolute path to this repo>/.env`
- Database: `conversation-warehouse`
- To query: `cd` to the scripts directory and use the venv python with `from _shared import get_db; client, db = get_db()` (returns tuple, not just db).
- Schema and collections are documented in the repo's README.md.
```

### 8. Add shell aliases

Add the following aliases if not already present.

macOS/Linux — append to ~/.zshrc or ~/.bashrc:

```bash
# Claude Code fork shortcuts
alias cfork='claude -r --fork-session'   # pick a session to fork-resume
alias cread='claude -c --fork-session'   # fork-continue latest session (for reading)
```

Windows (Git Bash) — append to ~/.bashrc:

```bash
# Claude Code fork shortcuts
alias cfork='claude -r --fork-session'
alias cread='claude -c --fork-session'
```

Windows (PowerShell) — append to $PROFILE:

```powershell
# Claude Code fork shortcuts
function cfork { claude -r --fork-session }
function cread { claude -c --fork-session }
```

### 9. Install /mongo skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/mongo/SKILL.md`
2. Use the template from `skills/mongo/SKILL.md` in this repo
3. Replace `{{VENV_PYTHON}}` with the absolute path to this repo's venv python
4. Replace `{{SCRIPT_DIR}}` with the absolute path to this repo's directory

This enables the `/mongo` slash command in Claude Code (e.g. `/mongo 2h` to see last 2 hours of activity).

### 10. Install /transplant skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/transplant/SKILL.md`
2. Use the template from `skills/transplant/SKILL.md` in this repo
3. Replace `{{VENV_PYTHON}}` with the absolute path to this repo's venv python
4. Replace `{{SCRIPT_DIR}}` with the absolute path to this repo's directory

This enables the `/transplant` slash command in Claude Code, which clones a session JSONL from one working directory into another (e.g. `/transplant <source.jsonl> <target-dir>`). The script lives at `session_transplant.py` in this repo.

### 11. Recommended environment variable

Add `CLAUDE_CODE_NO_FLICKER=1` to the `env` block in `~/.claude/settings.json`. This is the default behavior since Claude Code v2.1.89, but setting it explicitly future-proofs against the default ever flipping. It enables fullscreen rendering and the in-app `Ctrl+O → [` history-dump trick.

```json
{
  "env": {
    "CLAUDE_CODE_NO_FLICKER": "1"
  }
}
```

See `docs/scrollback.md` in this repo for the full explanation of fullscreen mode, scrollback behavior, and the `Ctrl+Home` / `Ctrl+O` navigation workflow.

### 12. Recommended VS Code terminal settings

If using Claude Code via VS Code's integrated terminal, add the following to your VS Code user settings (`%APPDATA%\Code\User\settings.json` on Windows, `~/.config/Code/User/settings.json` on Linux, `~/Library/Application Support/Code/User/settings.json` on Mac):

```json
{
  "terminal.integrated.scrollback": 250000,
  "terminal.integrated.persistentSessionScrollback": 250000,
  "terminal.integrated.gpuAcceleration": "canvas"
}
```

| Setting | Why |
|---|---|
| `scrollback: 250000` | Default 1000 is too small for any long Claude session. 250k uses ~40 MB per terminal max. Buffer grows lazily — no upfront cost. |
| `persistentSessionScrollback: 250000` | Default 100. Without this, scrollback shrinks to 100 lines on every VS Code window reload. Match it to `scrollback`. |
| `gpuAcceleration: "canvas"` | Default `"auto"` (WebGL) has lazy/batched repaints that cause `Ctrl+End` to require a follow-up keystroke to render. Canvas does immediate-mode painting which is more responsive. Costs almost nothing on modern hardware. |

Also add these editor settings:

```json
{
  "workbench.editor.enablePreview": false,
  "files.readonlyInclude": {
    "**/*": true
  }
}
```

| Setting | Why |
|---|---|
| `enablePreview: false` | Clicking a file in the sidebar opens it in its own tab instead of replacing the current preview tab. |
| `files.readonlyInclude: {"**/*": true}` | Opens all files in read-only mode by default (lock icon on tab). Prevents accidental edits. Toggle per-file with `Cmd+Shift+P` → "Toggle File Read-only". |

Also add these settings to disable VS Code's built-in Copilot/AI features (since we use Claude Code instead):

```json
{
  "workbench.startupEditor": "none",
  "workbench.welcomePage.enabled": false,
  "chat.disableAIFeatures": true,
  "chat.commandCenter.enabled": false,
  "chat.editor.enabled": false,
  "chat.agent.enabled": false,
  "github.copilot.enable": { "*": false },
  "github.copilot.chat.enabled": false,
  "github.copilot.completions.enabled": false,
  "github.copilot.nextEditSuggestions.enabled": false,
  "workbench.secondarySideBar.visible": false
}
```

| Setting | Why |
|---|---|
| `workbench.startupEditor: "none"` | Disables the welcome tab on startup. |
| `workbench.welcomePage.enabled: false` | Disables the welcome page entirely. |
| `chat.disableAIFeatures: true` | Nuclear option — disables and hides ALL built-in AI features (Copilot chat, inline suggestions, agent panel). Since VS Code 1.100+, Copilot is built-in and cannot be uninstalled as an extension. This is the only way to fully suppress it. |
| `chat.commandCenter.enabled: false` | Hides the AI chat button from the title bar. |
| `chat.editor.enabled: false` | Disables inline AI chat in the editor. |
| `chat.agent.enabled: false` | Disables the "Build with Agent" panel. |
| `github.copilot.enable: {"*": false}` | Disables Copilot for all file types. |
| `github.copilot.chat.enabled: false` | Disables the Copilot chat panel. |
| `github.copilot.completions.enabled: false` | Disables inline code completions. |
| `github.copilot.nextEditSuggestions.enabled: false` | Disables next-edit predictions. |
| `workbench.secondarySideBar.visible: false` | Hides the secondary sidebar where the Copilot chat panel lives. |

**Note:** Even with all these settings, the Copilot and Claude Code secondary sidebar may still appear when opening a **new folder** for the first time — this is a [known VS Code bug (#247175)](https://github.com/microsoft/vscode/issues/247175). VS Code stores sidebar visibility in per-workspace state, not in settings.json. Close it with `Cmd+Option+B` (Mac) or `Ctrl+Alt+B` (Windows) once per workspace — it stays closed after that.

Also recommended: globally unbind `Ctrl+O` in VS Code's `keybindings.json` so it passes through to Claude Code's terminal (otherwise VS Code intercepts it as "Open File" and the in-app `Ctrl+O → [` trick won't work):

```json
[
  {
    "key": "ctrl+o",
    "command": "-workbench.action.files.openFile"
  }
]
```

### 13. Verify

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
