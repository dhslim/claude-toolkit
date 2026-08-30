# Design Notes

Implementation decisions and their reasoning. See [README](../README.md) for the overview.


### SessionEnd "Hook cancelled" workaround
- Problem: SessionEnd default timeout 1.5s, MongoDB connect ~2.4s
- Solution: `hook_sync.py` spawns a detached process → exits immediately → child syncs in background

### Change detection (idempotency)
- `file_sync_cache` collection stores line count per file path
- Same line count → skip (no parsing needed)

### 16MB BSON limit
- Only SKIP_TYPES blacklist filtered (file-history-snapshot, progress, last-prompt, queue-operation)
- Everything else stored (tool_use input, tool_result output, thinking blocks)
- Auto-truncates if >14MB — keeps the **newest** messages, drops the oldest (`messages[-0.8:]`), flags the doc `truncated: true`

### Local transcript retention (`cleanupPeriodDays`)
- Claude Code auto-deletes transcripts from `~/.claude/projects` once they're older than `cleanupPeriodDays` (CC default: **30 days**), by last-activity date. Past that window a session can no longer be `--resume`d or forked locally.
- The platform `settings.json` templates set **`cleanupPeriodDays: 3650`** (~10 years) so local transcripts effectively persist — you keep resume/fork access to old sessions across all machines.
- MongoDB remains the **durable archive** regardless: the Stop/SessionEnd hooks sync each transcript to Mongo *before* CC's local cleanup runs, so Mongo is a **superset** of local disk (it retains sessions even after their local JSONL is purged). Raising `cleanupPeriodDays` only widens the *local* rolling window; it does not affect Mongo.
- Trade-off: longer local retention grows `~/.claude/projects` disk usage (a few GB/year at moderate volume). Lower the value if local disk gets tight — Mongo still has everything.
- Note: this only governs *future* cleanup; transcripts already purged under the old 30-day default survive only in Mongo (reconstruct locally from there if needed).

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

