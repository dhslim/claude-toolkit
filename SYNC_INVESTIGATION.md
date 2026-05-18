# MongoDB Sync Investigation

## Problem
Claude Code session data is not reliably syncing to MongoDB. The hook fires on every Stop event, but detached child processes that perform the actual sync silently fail — observed 24 fires with zero updates to MongoDB.

## Symptoms
- `/mongo 10m` returns 0 results even though active work was happening
- `synced_at` on the session document is stale (from last successful sync)
- No error output visible — failures are completely silent
- **Mac-specific observation (2026-05-18)**: User had multi-turn conversations on Macbook, expected to resume from Windows via `/mongo` recall — sessions were missing from MongoDB despite turns clearly happening locally.

## Architecture
1. Claude Code Stop hook triggers `hook_sync.py`
2. `hook_sync.py` spawns a detached child process to sync the session JSONL to MongoDB
3. The child process reads the local JSONL file, parses messages, and upserts to the `sessions` collection
4. `synced_at` is updated on success

## What We Know
- Manual execution of the sync script works every time — cannot reproduce the failure
- The detached child process is the failure point
- `hook_sync.py` was updated with logging (writes to `sync.log`) to capture the next failure
- Windows sync.log shows reliable per-turn syncs (`SYNC OK <sid>: updated (N msgs)` every Stop event)
- Mac sync.log is suspected to never get written (see Root Cause below)
- Log format:
  - `SYNC OK   <sid>: updated (N msgs)` — success
  - `SYNC SKIP <sid>: no session_id or messages` — empty file
  - `SYNC ERR  <sid>: <traceback>` — failure with full error
  - `SYNC FAIL <sid>: file not found: <path>` — missing file

## Root Cause (Mac, identified 2026-05-18)

`_sync_runner.py` used `import msvcrt` unconditionally inside `log()`. `msvcrt` is a **Windows-only** stdlib module — on macOS/Linux, the import raises `ModuleNotFoundError`.

Failure chain on Mac:
1. Stop hook fires → `hook_sync.py` spawns detached `_sync_runner.py`
2. `_sync_runner.main()` runs, attempts to log start/result
3. `log()` calls `import msvcrt` → `ModuleNotFoundError`
4. If error occurs before `sync_one_file()` was called, **no sync happens**
5. If error occurs after sync succeeds, sync is fine but log entry is lost, then the outer `except` block tries to log the error and that also fails — child dies silently

Either way, the user sees:
- No log entries in `sync.log` on Mac
- No update to MongoDB

## Fix (committed 2026-05-18)

Made file locking cross-platform in `_sync_runner.py`:
- Windows → `msvcrt.locking()` (existing behavior)
- Mac/Linux → `fcntl.flock()` (new)

Helper functions `_acquire_lock()` / `_release_lock()` dispatch based on `sys.platform == 'win32'`.

## Hypotheses (original — now mostly resolved)
1. ~~Process detachment issue on Windows~~ — Windows works reliably; not a Windows-detachment problem
2. ~~File locking~~ — locking ITSELF wasn't the issue, but the **Windows-only locking import** broke Mac
3. ~~Python path / venv issue~~ — venv is selected per-platform in `hook_sync.py`; not the cause
4. **Race condition** — still possible but secondary; primary issue was the import error

## Next Steps
- [x] Wait for next failure and check `sync.log` for the actual error — Mac log was empty (confirms import-time failure)
- [x] Identify Mac-specific failure mode — `msvcrt` import error
- [x] Fix cross-platform locking
- [ ] Deploy fix to Mac install (pull from origin/main on Mac)
- [ ] Verify Mac sync.log starts populating after deploy
- [ ] Cross-machine recall test: have conversation on Mac, recall from Windows via `/mongo`

## Timeline
- 2026-04-06: Issue first observed in DentWebMigration session. Logging added to hook_sync.py.
- 2026-05-18: Mac-specific root cause identified (`msvcrt` import on non-Windows). Cross-platform fix applied to `_sync_runner.py`.
