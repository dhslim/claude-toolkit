# Conversation Warehouse

Auto-sync Claude Code conversation history to MongoDB Atlas and generate daily review quizzes via hooks.

## Quick Start

```bash
git clone https://github.com/dhslim/claude-toolkit.git
cd claude-toolkit
```

Then run the setup script for your platform:

| Platform | Command |
|----------|---------|
| **Linux** | `bash platform/linux/setup.sh` |
| **macOS** | `bash platform/macos/setup.sh` |
| **Windows** | `powershell -ExecutionPolicy Bypass -File platform\windows\setup.ps1` |

Each setup script installs dependencies, creates `.env`, and prints the hook configuration to add to `~/.claude/settings.json`.

## Structure

```
claude-toolkit/
├── sync-conversations.js    # Core sync script (--file, --scan modes)
├── hook-sync.js             # Stop/SessionEnd hook wrapper (detached process)
├── quiz-check.js            # SessionStart hook — daily quiz gate
├── quiz-data.js             # Fetch yesterday's conversations from MongoDB
├── quiz-save.js             # Save generated quiz to MongoDB
├── quiz-mark-done.js        # Mark quiz as completed
├── daily-quiz.js            # Standalone quiz generator (uses Anthropic API)
├── dump-stdin.js            # Debug: dump hook stdin to file
├── package.json
├── .env.example
└── platform/
    ├── linux/
    │   ├── setup.sh
    │   └── README.md
    ├── macos/
    │   ├── setup.sh
    │   └── README.md
    └── windows/
        ├── setup.ps1
        ├── setup-scheduled-task.ps1
        └── README.md
```

## Where to Clone

The repo can live anywhere on your system. The setup scripts use absolute paths based on where you cloned it:

| Platform | Suggested location |
|----------|--------------------|
| **Linux** | `~/scripts/claude-toolkit` or `~/claude-toolkit` |
| **macOS** | `~/scripts/claude-toolkit` or `~/claude-toolkit` |
| **Windows** | `C:\Users\<you>\scripts\claude-toolkit` |

The hook commands in `~/.claude/settings.json` will point to the actual clone location, so it works from anywhere.

## Sync Flow

3 hooks cover all sync scenarios — no cron needed:

| Hook | Trigger | Action | Covers |
|------|---------|--------|--------|
| **Stop** | After Claude responds | Sync current session | Normal flow (99%) |
| **SessionEnd** | `/exit` or terminal close | Sync current session | Exit after interruption |
| **SessionStart** | Every Claude start | Full scan (`--scan`) | Sessions missed by force-quit |

### Why no cron?

- Stop → syncs after every response
- SessionEnd → syncs on exit
- Force-quit → next SessionStart full-scan catches missed sessions
- Only gap: force-quit then never open Claude again (negligible)

## Hook Configuration (`~/.claude/settings.json`)

Replace `TOOLKIT_DIR` with your actual clone path:

```json
{
  "hooks": {
    "Stop": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node TOOLKIT_DIR/hook-sync.js",
      "async": true, "timeout": 30000
    }]}],
    "SessionEnd": [{ "matcher": "", "hooks": [{
      "type": "command",
      "command": "node TOOLKIT_DIR/hook-sync.js",
      "timeout": 10000
    }]}],
    "SessionStart": [{ "matcher": "", "hooks": [
      { "type": "command",
        "command": "node TOOLKIT_DIR/sync-conversations.js --scan",
        "async": true, "timeout": 60000 },
      { "type": "command",
        "command": "node TOOLKIT_DIR/quiz-check.js",
        "timeout": 3000 }
    ]}]
  }
}
```

## Key Design Decisions

### SessionEnd "Hook cancelled" workaround
- Problem: SessionEnd default timeout 1.5s, MongoDB connect ~2.4s
- Solution: `hook-sync.js` spawns a detached process → exits immediately → child syncs in background

### Change detection (idempotency)
- `file_sync_cache` collection stores line count per file path
- Same line count → skip (no parsing needed)

### 16MB BSON limit
- Only SKIP_TYPES blacklist filtered (file-history-snapshot, progress, last-prompt, queue-operation)
- Everything else stored (tool_use input, tool_result output, thinking blocks)
- Auto-truncates if >14MB

### `claude -c` deduplication
- `claude -c` / `claude --resume` → appends to same JSONL file (same session_id)
- No separate file created → no duplicate storage

## Daily Quiz

### Flow
1. SessionStart runs `quiz-check.js`
2. If today's quiz not taken → injects quiz instructions into Claude context
3. Claude runs `quiz-data.js` to fetch yesterday's conversations
4. Claude generates 10 questions, saves via `quiz-save.js`
5. User answers, Claude grades, then `quiz-mark-done.js` marks complete
6. Same-day restarts skip the quiz

### Features
- No Anthropic API key needed — Claude Code itself generates the quiz
- Non-blocking — background agent prepares while user shares their task
- Once per day — marker file prevents repeats

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
  messages: [{ type, role, content, timestamp, uuid }]
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

## Debugging

```bash
# Check sync log
tail -20 sync.log

# Count sessions in MongoDB
node -e "
require('dotenv').config();
const {MongoClient}=require('mongodb');
(async()=>{const c=new MongoClient(process.env.MONGODB_URI);
await c.connect();
console.log(await c.db('conversation-warehouse').collection('sessions').countDocuments());
await c.close()})();"

# Manual full sync
node sync-conversations.js --scan
```
