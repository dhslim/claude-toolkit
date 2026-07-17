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

If .env already exists, skip the creation step.

In all cases (whether .env was just created or already existed), restrict its permissions so only the owner can read it:

  chmod 600 .env  # macOS/Linux
  icacls .env /inheritance:r /grant:r "%USERNAME%:F"  # Windows

The MongoDB URI contains the database password embedded in plain text (`mongodb+srv://user:password@cluster.../`). On a shared machine, default `644` permissions would let other users on the same host read your DB password. `600` (owner read/write only) prevents this.

### 4. Test the connection

Run sync_conversations.py --scan using the venv python to verify MongoDB connectivity.

If it fails with a TLS/SSL error, the Python or OpenSSL version may be too old. Upgrade Python to 3.10+ and retry.

### 5. Configure ~/.claude/settings.json

This repo has platform-specific settings templates in `platform/<os>/settings.json`:
- **Windows**: `platform/windows/settings.json`
- **Linux**: `platform/linux/settings.json`
- **macOS**: `platform/macos/settings.json`

Pick the template that matches the current OS. Create or update `~/.claude/settings.json` to match its structure, but **replace all `{{REPO_DIR}}` placeholders** with the absolute path to this repo's directory.

On Windows there are two python executables in the venv:
- pythonw.exe — runs without opening a console window (use for background/async hooks)
- python.exe — runs with stdout visible (use when Claude needs to read the hook's output)

On macOS/Linux use the single `python` executable in `.venv/bin/`.

Preserve any existing settings in ~/.claude/settings.json that are not in the reference file.

### 6. Install status line

Copy `statusline.sh` from this repo to `~/.claude/statusline.sh` and make it executable (`chmod +x`).

The settings.json already references this path via `~/.claude/statusline.sh`.

Toolkit-drift notifier: the SessionStart hook `hook_toolkit_drift.py` (wired via the platform `settings.json` in step 5) warns you when this repo is behind `origin/main`. It reads a cached "N behind" count instantly at session start and, if behind, tells Claude to suggest `git pull`; a detached ~24h-throttled `git fetch` refreshes that cache off the startup path. It never pulls on its own; opt out with `TOOLKIT_NO_UPDATE_CHECK=1`.

### 7. Update global CLAUDE.md

Read ~/.claude/CLAUDE.md (create it if it doesn't exist). Ensure ALL blocks below are present — for each, if its section is missing append it; if it already exists, update it to match. Preserve all other existing content.

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

This second block pairs with the `hook_inject_time.py` UserPromptSubmit hook from the platform `settings.json` in step 5 (the hook injects the time; this rule tells Claude to print it). Without it, the timestamp is injected but never shown.

```
## Response timestamp
- Every turn, a UserPromptSubmit hook (`hook_inject_time.py`, wired via step 5's settings.json) injects a `<current-time>YYYY-MM-DD HH:MM:SS KST</current-time>` line plus a `<reminder>` into the prompt context. Injecting the reminder every turn (not only here) keeps it at the most-recent context position so it is far less likely to be dropped on long sessions.
- A Stop hook (`hook_enforce_timestamp.py`, also wired in step 5) is the deterministic backstop: when Claude ends a turn without the closing stamp, it blocks the stop and makes Claude append it. It is loop-safe via `stop_hook_active` (at most one extra round-trip per turn) and uses Windows `python.exe` (not `pythonw.exe`) so Claude can read its block decision from stdout.
- End EVERY response with that injected timestamp on its own final line, wrapped in single backticks so it renders as inline code (distinct color/font) in the transcript — like: `2026-06-07 09:10:09 KST`
- Use the injected value verbatim; never guess the wall-clock time. If no `<current-time>` was injected this turn, omit the stamp rather than inventing one.
- No emoji anywhere in responses — terminals here use cp949/utf-8 and emoji corrupt the output.
```

This third block is a standalone preference (no hook dependency).

```
## File References
- Always give the full, complete absolute path for every file or directory you mention — never abbreviate, truncate, or collapse any part with `...`. Write the whole path from the drive root (e.g. `C:\Users\user\Desktop\THINGS\desktop\admin stuff\file.docx`) so the user can ctrl+click to open it.
- Applies everywhere: prose, summaries, tables, code blocks, and bullet lists.
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

### 9. Install /mgo skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/mgo/SKILL.md`
2. Use the template from `skills/mgo/SKILL.md` in this repo
3. Replace `{{VENV_PYTHON}}` with the absolute path to this repo's venv python
4. Replace `{{SCRIPT_DIR}}` with the absolute path to this repo's directory

This enables the `/mgo` slash command in Claude Code (e.g. `/mgo 2h` to see last 2 hours of activity).

### 10. Install /transplant skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/transplant/SKILL.md`
2. Use the template from `skills/transplant/SKILL.md` in this repo
3. Replace `{{VENV_PYTHON}}` with the absolute path to this repo's venv python
4. Replace `{{SCRIPT_DIR}}` with the absolute path to this repo's directory

This enables the `/transplant` slash command in Claude Code, which clones a session JSONL from one working directory into another (e.g. `/transplant <source.jsonl> <target-dir>`). The script lives at `session_transplant.py` in this repo.

### 11. Install /pushback skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/pushback/SKILL.md`
2. Use the template from `skills/pushback/SKILL.md` in this repo as-is (no placeholder substitution needed — this skill is pure instruction text, no script paths)

This enables the `/pushback` slash command in Claude Code. Running `/pushback <message>` engages critically with that specific claim or proposal; running `/pushback` alone re-examines the assistant's previous response and surfaces its weakest points. Designed to counteract the default helpful-assistant tendency to nod along when it should challenge.

### 12. Install /grill-me skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/grill-me/SKILL.md`
2. Use the template from `skills/grill-me/SKILL.md` in this repo as-is (no placeholder substitution needed — pure instruction text)

This enables the `/grill-me` slash command in Claude Code, an interactive quiz where Claude grills you on a topic, file, PR, diff, commit, or concept to test and deepen your understanding — optionally calibrated with a difficulty/mode hint (`hard`, `brutal`, `interview`, `quick`, `rapid fire`). E.g. `/grill-me this PR`, `/grill-me sync_conversations.py hard`, `/grill-me "MongoDB write concern" interview`. Sibling to `/pushback`: `/pushback` argues against *your* claim in one response; `/grill-me` quizzes *you* across many turns.

### 13. Install /digest skill

Copy the skill template from this repo to the global skills directory:

1. Create `~/.claude/skills/digest/SKILL.md`
2. Use the template from `skills/digest/SKILL.md` in this repo
3. Replace `{{VENV_PYTHON}}` with the absolute path to this repo's venv python
4. Replace `{{SCRIPT_DIR}}` with the absolute path to this repo's directory

This enables the `/digest <url>` slash command, which fetches and summarizes any supported social/web post (YouTube, Instagram, Threads, Reddit, Substack/blogs/news) via `digest.py` and the `fetchers/` package. Its extra dependencies (`yt-dlp`, `instaloader`) are already in requirements.txt, so step 2 covers them.

### 14. Recommended environment variable

`CLAUDE_CODE_NO_FLICKER=1` is already included in the `env` block of the platform settings templates (`platform/linux/settings.json`, `platform/macos/settings.json`, `platform/windows/settings.json`), so a standard install via step 3 picks it up automatically — no manual step needed. This is the default behavior since Claude Code v2.1.89, but setting it explicitly future-proofs against the default ever flipping. It enables fullscreen rendering and the in-app `Ctrl+O → [` history-dump trick.

See `docs/scrollback.md` in this repo for the full explanation of fullscreen mode, scrollback behavior, and the `Ctrl+Home` / `Ctrl+O` navigation workflow.

### 15. Apply VS Code settings + keybindings

This step **applies** recommended VS Code settings and keybindings to the user's local config — it doesn't just document them. Skip this step if Claude Code isn't being used via VS Code's integrated terminal.

**Important:** VS Code stores user settings on the machine where the IDE runs, not on a remote server. If this install is being run via VS Code Remote-SSH, you must run it on the LOCAL machine for these file paths to resolve correctly. If you can't reach the user's local settings file (e.g., you're on the remote side of an SSH session), skip this step and tell the user to run the install locally on their laptop for steps 15a and 15b.

#### Locate the two files (OS-dependent)

| OS | settings.json | keybindings.json |
|---|---|---|
| Windows | `%APPDATA%\Code\User\settings.json` | `%APPDATA%\Code\User\keybindings.json` |
| macOS | `~/Library/Application Support/Code/User/settings.json` | `~/Library/Application Support/Code/User/keybindings.json` |
| Linux | `~/.config/Code/User/settings.json` | `~/.config/Code/User/keybindings.json` |

If either file doesn't exist, create the parent directory and start from `{}` for settings.json or `[]` for keybindings.json.

#### 15a. Merge into settings.json

Read the existing settings.json. **Preserve all existing keys that aren't in the list below** — only overwrite the specific keys here. Then write back:

```jsonc
{
  // Terminal — scrollback and rendering
  "terminal.integrated.scrollback": 250000,
  "terminal.integrated.persistentSessionScrollback": 250000,
  "terminal.integrated.gpuAcceleration": "canvas",

  // Editor behavior
  "workbench.editor.enablePreview": false,
  "files.readonlyInclude": {
    "**/*": true
  },

  // Disable built-in Copilot/AI features (we use Claude Code instead)
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

VS Code's settings.json supports JSON-with-comments (JSONC). If the existing file has comments, preserve them when possible. If your tooling can't preserve comments cleanly, ask the user before stripping them, and offer to back up the original to `settings.json.bak` first.

#### Why each settings.json entry

| Setting | Why |
|---|---|
| `terminal.integrated.scrollback: 250000` | Default 1000 is too small for any long Claude session. 250k uses ~40 MB per terminal max. Buffer grows lazily — no upfront cost. |
| `terminal.integrated.persistentSessionScrollback: 250000` | Default 100. Without this, scrollback shrinks to 100 lines on every VS Code window reload. Match it to `scrollback`. |
| `terminal.integrated.gpuAcceleration: "canvas"` | Default `"auto"` (WebGL) has lazy/batched repaints that cause `Ctrl+End` to require a follow-up keystroke to render. Canvas does immediate-mode painting which is more responsive. Costs almost nothing on modern hardware. |
| `workbench.editor.enablePreview: false` | Clicking a file in the sidebar opens it in its own tab instead of replacing the current preview tab. |
| `files.readonlyInclude: {"**/*": true}` | Opens all files in read-only mode by default (lock icon on tab). Prevents accidental edits. Toggle per-file with `Cmd+Shift+P` → "Toggle File Read-only". |
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

**Note:** Even with all these settings, the Copilot/Claude Code secondary sidebar may still appear when opening a **new folder** for the first time — known [VS Code bug #247175](https://github.com/microsoft/vscode/issues/247175). VS Code stores sidebar visibility in per-workspace state, not in settings.json. The user can close it once per workspace with `Cmd+Option+B` (Mac) / `Ctrl+Alt+B` (Windows) and it stays closed.

#### 15b. Merge into keybindings.json

keybindings.json is a JSON array of binding objects, not an object. Read it, then **for each binding below, remove any existing entry with the same `key` AND same `command` before appending** (to avoid duplicates if the install runs again). Then write back.

Bindings to add on **all** platforms:

```jsonc
[
  // Unbind Ctrl+O so the terminal receives it (for Claude Code's Ctrl+O → [ transcript dump)
  {
    "key": "ctrl+o",
    "command": "-workbench.action.files.openFile"
  }
]
```

Bindings to add **only on macOS**:

```jsonc
[
  // Make Cmd+J FOCUS the terminal instead of TOGGLING it
  {
    "key": "cmd+j",
    "command": "workbench.action.terminal.focus",
    "when": "!terminalFocus"
  },
  {
    "key": "cmd+j",
    "command": "-workbench.action.togglePanel"
  }
]
```

Bindings to add **only on Windows/Linux**:

```jsonc
[
  // Make Ctrl+J FOCUS the terminal instead of TOGGLING it
  {
    "key": "ctrl+j",
    "command": "workbench.action.terminal.focus",
    "when": "!terminalFocus"
  },
  {
    "key": "ctrl+j",
    "command": "-workbench.action.togglePanel"
  }
]
```

#### Why each keybinding

| Keybinding | Why |
|---|---|
| Unbind `Ctrl+O` | VS Code's default `Ctrl+O` is "Open File", which intercepts the keystroke before it reaches the terminal. Without unbinding it, `Ctrl+O → [` (Claude Code's transcript dump) won't work. |
| `Cmd+J` / `Ctrl+J` → focus terminal | Default behavior toggles the panel (shows/hides). This changes it to always focus the terminal without ever hiding it. When running Claude Code in the terminal, you never want this key combo to hide the terminal — you just want to jump to it. |

#### Verification

After writing both files, tell the user: "VS Code settings + keybindings applied. **Reload the VS Code window** (`Cmd/Ctrl+Shift+P` → 'Developer: Reload Window') for them to take effect."

### 16. Verify

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
