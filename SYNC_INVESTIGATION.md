# MongoDB Sync Investigation

## Problem
Claude Code session data is not reliably syncing to MongoDB. The hook fires on every Stop event, but detached child processes that perform the actual sync silently fail — observed 24 fires with zero updates to MongoDB.

## Symptoms
- `/mongo 10m` returns 0 results even though active work was happening
- `synced_at` on the session document is stale (from last successful sync)
- No error output visible — failures are completely silent

## Architecture
1. Claude Code Stop hook triggers `hook_sync.py`
2. `hook_sync.py` spawns a detached child process to sync the session JSONL to MongoDB
3. The child process reads the local JSONL file, parses messages, and upserts to the `sessions` collection
4. `synced_at` is updated on success

## What We Know
- Manual execution of the sync script works every time — cannot reproduce the failure
- The detached child process is the failure point
- `hook_sync.py` was updated with logging (writes to `sync.log`) to capture the next failure
- Log format:
  - `SYNC OK   <sid>: updated (N msgs)` — success
  - `SYNC SKIP <sid>: no session_id or messages` — empty file
  - `SYNC ERR  <sid>: <traceback>` — failure with full error
  - `SYNC FAIL <sid>: file not found: <path>` — missing file

## Hypotheses
1. **Process detachment issue on Windows** — `subprocess.Popen` with `CREATE_NEW_PROCESS_GROUP` / `DETACHED_PROCESS` flags may not inherit environment or working directory correctly in some cases
2. **File locking** — the JSONL file may be locked by Claude Code when the hook fires, and the detached child reads it too early or too late
3. **Python path / venv issue** — the detached child may not find the correct Python or dependencies
4. **Race condition** — multiple hooks firing in quick succession could cause conflicts

## Next Steps
- [ ] Wait for next failure and check `sync.log` for the actual error
- [ ] If no error appears in log, the child process may be dying before it even starts logging — add a "process started" marker at the very top of the child script
- [ ] Consider switching from detached child to synchronous execution (slower but reliable) as a fallback

## Timeline
- 2026-04-06: Issue first observed in DentWebMigration session. Logging added to hook_sync.py.
