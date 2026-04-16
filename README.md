# Conversation Warehouse

Auto-sync Claude Code conversation history to MongoDB Atlas and generate daily review quizzes via hooks.

## Install

```bash
git clone https://github.com/dhslim/claude-toolkit.git
```

Then open Claude Code and paste the install prompt from [INSTALL.md](INSTALL.md). Claude Code handles the entire setup — venv, dependencies, `.env`, and hooks — on any OS.

## Structure

```
claude-toolkit/
├── sync_conversations.py  # Core sync script (--file, --scan modes)
├── hook_sync.py           # Stop/SessionEnd hook wrapper (detached process)
├── hook_session_guard.py   # SessionStart hook — warns if session already open
├── hook_session_guard_cleanup.py  # SessionEnd hook — removes session lock
├── quiz_check.py          # Stop hook — daily quiz gate
├── quiz_dismiss.py        # Dismiss quiz for today
├── quiz_data.py           # Fetch yesterday's conversations from MongoDB
├── quiz_save.py           # Save generated quiz to MongoDB
├── quiz_grade.py          # Grade quiz, save score, and mark done (atomic)
├── quiz_mark_done.py      # Mark quiz as completed (legacy, replaced by quiz_grade.py)
├── daily_quiz.py          # Standalone quiz data fetcher (supports --date arg)
├── hook_notify.py         # Stop hook — cross-platform notification sound (>30s turns)
├── hook_turn_start.py     # UserPromptSubmit hook — records turn start timestamp
├── mongo_recent.py        # Query recent activity across all machines (/mongo skill)
├── session_transplant.py  # Clone a session JSONL into a different cwd (/transplant skill)
├── _shared.py             # Shared utilities (DB connection, retry, KST timezone)
├── requirements.txt       # pymongo, python-dotenv
├── .env.example
├── INSTALL.md             # Install prompt for Claude Code
├── platform/
│   ├── windows/settings.json  # Hook config template (pythonw.exe / python.exe)
│   ├── linux/settings.json    # Hook config template (.venv/bin/python)
│   └── macos/settings.json    # Hook config template (.venv/bin/python)
└── README.md
```

## Sync Flow

Hooks cover all sync scenarios — no cron needed:

| Hook | Trigger | Action | Covers |
|------|---------|--------|--------|
| **Stop** | After Claude responds | Sync current session + quiz check | Normal flow (99%) |
| **SessionEnd** | `/exit` or terminal close | Sync current session + remove session lock | Exit after interruption |
| **SessionStart** | Every Claude start | Session guard + full scan (`--scan`) | Duplicate session warning + missed sessions |

### Why no cron?

- Stop → syncs after every response
- SessionEnd → syncs on exit
- Force-quit → next SessionStart full-scan catches missed sessions
- Only gap: force-quit then never open Claude again (negligible)

## Key Design Decisions

### SessionEnd "Hook cancelled" workaround
- Problem: SessionEnd default timeout 1.5s, MongoDB connect ~2.4s
- Solution: `hook_sync.py` spawns a detached process → exits immediately → child syncs in background

### Change detection (idempotency)
- `file_sync_cache` collection stores line count per file path
- Same line count → skip (no parsing needed)

### 16MB BSON limit
- Only SKIP_TYPES blacklist filtered (file-history-snapshot, progress, last-prompt, queue-operation)
- Everything else stored (tool_use input, tool_result output, thinking blocks)
- Auto-truncates if >14MB

### Session forking behavior (same-machine concurrent resume)
- When two terminals `claude --resume` the same session, both append to the **same JSONL file**
- No file locking — relies on `O_APPEND` atomicity (safe on macOS APFS)
- Each message has a `uuid` and `parentUuid`, forming a tree (not a flat list)
- A fork creates a branch point: two messages share the same `parentUuid`
- **Claude Code `--resume`**: walks the `parentUuid` chain backward from the last-written message → only shows one fork. The other fork's messages are in the file but invisible to the user
- **MongoDB sync**: reads the JSONL flat (ignores `parentUuid`) → stores the **superset** of all forks in chronological order. No data loss
- Interleaving in MongoDB is minimal in practice since forks are rare (deliberate concurrent resume only)
- When reading session transcripts from MongoDB, be aware that forked sessions may have interleaved messages from separate conversation branches

### Session guard (duplicate session warning)
- `hook_session_guard.py` runs on SessionStart — checks if another Claude process already has the same session open
- Uses PID lock files in `~/.claude/session-locks/` to track active sessions
- If a collision is detected, emits a **blocking warning** suggesting `cfork` or `cread` instead
- User can dismiss the warning to continue anyway (lock file is then updated to the new process)
- `hook_session_guard_cleanup.py` runs on SessionEnd to remove the lock file
- Stale locks from crashed sessions are handled automatically via PID liveness check

### Safe session reading with `--fork-session`
Claude Code's `--fork-session` flag creates a new session ID on resume, protecting the original session from accidental writes.

**Recommended shell aliases** (add to `~/.zshrc`):
```bash
alias cfork='claude -r --fork-session'   # pick a session to fork-resume
alias cread='claude -c --fork-session'   # fork-continue latest session (for reading)
```

**Exit behavior with `--fork-session`** (verified experimentally):

| Action | New JSONL created? | Side effects? |
|--------|-------------------|---------------|
| `cfork` + Ctrl+C | No | None |
| `cread` + Ctrl+C | No | None |
| `cfork` + `/rename` + `/exit` | Yes | New session in MongoDB |
| `cread` + `/rename` + `/exit` | Yes | New session in MongoDB |

**Rule: Always close fork sessions with Ctrl+C to avoid creating duplicate sessions.**

### Notification sound (cross-platform)
- Plays a notification sound when a turn takes longer than 30 seconds
- `hook_turn_start.py` (UserPromptSubmit) records turn start time to `~/.claude/turn-start`
- `hook_notify.py` (Stop) checks elapsed time and plays sound only if > 30s
- macOS: Glass.aiff, Windows: custom notify.wav. Linux/SSH not supported (no audio output)
- Short turns (active chatting) produce no sound — avoids annoyance when focused

### Quiz triggers on Stop only
- Stop fires after every response — guaranteed to hit
- Markers stored in MongoDB (global, shared across machines) + local files (fast cache)
- Local cache checked first (0ms) → MongoDB only on cache miss (once/day/machine)
- User can explicitly dismiss the quiz for the day ("skip quiz", "not now")

## Daily Quiz

### Flow
1. `quiz_check.py` runs on Stop hook (after each Claude response)
2. If today's quiz not taken or dismissed → injects quiz instructions into Claude context
3. Claude runs `quiz_data.py` to fetch yesterday's conversations (200K char budget, evenly distributed across sessions)
4. Claude generates 10 questions, saves via `quiz_save.py` (choices auto-shuffled to prevent answer position bias)
5. User answers, Claude pipes answers to `quiz_grade.py` which grades, saves score, and marks complete atomically
6. Same-day restarts skip the quiz (user can also dismiss with "skip quiz")

### Features
- No Anthropic API key needed — Claude Code itself generates the quiz
- Non-blocking — background agent prepares while user shares their task
- Once per day — global markers in MongoDB prevent repeats across machines
- Local file cache ensures near-zero latency on repeated checks
- Even session coverage — 200K char budget split equally so no single session dominates
- Answer randomization — `quiz_save.py` shuffles choice order to eliminate LLM position bias

## `/mongo` Skill — Recent Activity Viewer

Custom Claude Code slash command to query recent activity across all machines.

```
/mongo 10      # last 10 minutes (default unit)
/mongo 2h      # last 2 hours
/mongo 1d      # last 1 day
```

- Always runs as a background task — type your follow-up prompt immediately
- Queries MongoDB for sessions with recent `synced_at`, then filters messages by timestamp
- Returns data across all devices (Mac, Windows, GPU servers, SSH sessions)
- Claude summarizes the results: projects, topics, decisions, code changes
- Skill file: `~/.claude/skills/mongo/SKILL.md`
- Query script: `mongo_recent.py`

## `/transplant` Skill — Session Cloner

Clone a Claude Code session JSONL from one working directory to another, so the cloned session shows up in `claude -r` from the target directory as if it had been created there.

```
/transplant <source.jsonl> <target-directory>
```

What it rewrites in the clone:
- Fresh `sessionId` UUID (zero identity overlap with the source — they coexist)
- Per-line `cwd` field
- Per-line `gitBranch` (auto-detected from sibling sessions in the target encoded dir)
- First user message flattened from list-form to plain string (otherwise the picker hides the session from the default "current worktree" view)

What it leaves alone (intentional):
- The source file itself — completely untouched
- Tool result content strings (cosmetic stale paths in transcript)
- `uuid`, `parentUuid`, `version`, `timestamp` (message-level identity chain)

- Skill file: `~/.claude/skills/transplant/SKILL.md`
- Script: `session_transplant.py`

## MongoDB Schema

### sessions collection
```js
{
  session_id: "uuid",                  // unique index
  session_name: "stt-architecture",    // from /rename (JSONL custom-title)
  project: "/home/user/myproject",     // cwd from session
  device: "hostname",
  session_date: ISODate,               // index
  synced_at: ISODate,
  message_count: Number,
  raw_line_count: Number,
  messages: [{ type, role, content, timestamp, uuid, parentUuid }]
}
```

### file_sync_cache collection
```js
{
  file_path: "/normalized/path.jsonl", // unique index
  line_count: Number,
  synced_at: ISODate
}
```

### quiz-markers collection
```js
{
  date: "2026-03-19",              // unique index (KST date)
  taken_at: ISODate | null,        // when quiz was completed
  dismissed_at: ISODate | null     // when quiz was dismissed
}
```

## Debugging

```bash
# Check sync log
tail -20 sync.log

# Manual full sync (from repo directory, using venv python)
.venv/bin/python sync_conversations.py --scan
```

## Documentation

In-depth references for Claude Code internals discovered while building and using the toolkit:

- **[`docs/sessions.md`](docs/sessions.md)** — Claude Code session storage on disk: where JSONLs live, the JSONL line format, the dual filesystem-location/per-line-`cwd` binding, the resume picker filter rules, per-worktree branch-detection quirks, and the full transplant checklist that powers `/transplant`.
- **[`docs/scrollback.md`](docs/scrollback.md)** — Claude Code's fullscreen rendering, the `CLAUDE_CODE_NO_FLICKER` env var, the alt-buffer scrollback bypass, and the workflow for actually scrolling back through long conversations: `Ctrl+Home` for in-viewport navigation (the simple answer) and `Ctrl+O → [` for dumping to native terminal scrollback (the power-user move). Includes a complete reference of interactive-mode keyboard shortcuts.
- **[`docs/hacks.md`](docs/hacks.md)** — quick-reference hacks (older manual session-import approach, etc.).
- **[`docs/quiz-considerations.md`](docs/quiz-considerations.md)** — daily quiz design notes.
