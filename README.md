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
├── quiz_check.py          # Stop hook — daily quiz gate
├── quiz_dismiss.py        # Dismiss quiz for today
├── quiz_data.py           # Fetch yesterday's conversations from MongoDB
├── quiz_save.py           # Save generated quiz to MongoDB
├── quiz_mark_done.py      # Mark quiz as completed
├── daily_quiz.py          # Standalone quiz generator (uses Anthropic API)
├── _shared.py             # Shared utilities (DB connection, retry, KST timezone)
├── requirements.txt       # pymongo, python-dotenv
├── .env.example
├── INSTALL.md             # Install prompt for Claude Code
└── README.md
```

## Sync Flow

3 hooks cover all sync scenarios — no cron needed:

| Hook | Trigger | Action | Covers |
|------|---------|--------|--------|
| **Stop** | After Claude responds | Sync current session + quiz check | Normal flow (99%) |
| **SessionEnd** | `/exit` or terminal close | Sync current session | Exit after interruption |
| **SessionStart** | Every Claude start | Full scan (`--scan`) | Sessions missed by force-quit |

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

### `claude -c` deduplication
- `claude -c` / `claude --resume` → appends to same JSONL file (same session_id)
- No separate file created → no duplicate storage

### Quiz triggers on Stop only
- Stop fires after every response — guaranteed to hit
- Markers stored in MongoDB (global, shared across machines) + local files (fast cache)
- Local cache checked first (0ms) → MongoDB only on cache miss (once/day/machine)
- User can explicitly dismiss the quiz for the day ("skip quiz", "not now")

## Daily Quiz

### Flow
1. `quiz_check.py` runs on Stop hook (after each Claude response)
2. If today's quiz not taken or dismissed → injects quiz instructions into Claude context
3. Claude runs `quiz_data.py` to fetch yesterday's conversations
4. Claude generates 10 questions, saves via `quiz_save.py`
5. User answers, Claude grades, then `quiz_mark_done.py` marks complete
6. Same-day restarts skip the quiz (user can also dismiss with "skip quiz")

### Features
- No Anthropic API key needed — Claude Code itself generates the quiz
- Non-blocking — background agent prepares while user shares their task
- Once per day — global markers in MongoDB prevent repeats across machines
- Local file cache ensures near-zero latency on repeated checks

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
