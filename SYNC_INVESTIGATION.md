# MongoDB Sync Investigation

## Problem
Claude Code session data sometimes doesn't reach MongoDB. The Stop hook is designed to spawn a detached child that syncs the session JSONL on every turn, but in practice many turns appear to not result in an updated `synced_at` in MongoDB.

## Symptoms
- `/mongo 10m` sometimes returns 0 results even though active work was happening
- `synced_at` on session documents is sometimes hours/days stale
- No error output visible — failures are silent
- **Mac-specific observation (2026-05-18)**: User had multi-turn conversations on Macbook and expected to resume from Windows via `/mongo` recall — some sessions were missing or had stale `synced_at`.

## Architecture
1. Claude Code Stop hook triggers `hook_sync.py`
2. `hook_sync.py` spawns a detached child process to sync the session JSONL to MongoDB
3. The child process reads the local JSONL file, parses messages, and upserts to the `sessions` collection
4. `synced_at` is updated on success

## Empirical Data (2026-05-18)

Per-session lag analysis (`last_msg.timestamp` → `synced_at`):

### Windows (DESKTOP-0DR960U)
- Active session `f379620a`: lag **0.3 seconds** ← per-turn working perfectly
- Recent sessions `50ec9477`, `df41a22c`, `c808ab98`: lag < 2 minutes
- Old session `c85cd2e8`: lag **501 hours** (~20 days) — last_msg in April, synced in May; likely re-synced via `--scan` after file metadata changed

### Mac (Davids-MacBook-Pro-4.local)
- `d84f0892`: lag **0.0 minutes** ← per-turn working
- `6c5a54ab`, `d0fe32a3`: lag < 1 minute ← per-turn working
- `a118517f`: lag 52 minutes ← partial
- `f1eb21ee`: lag 22.5 hours ← delayed
- `16e904a6`: lag **365 hours** (~15 days) ← very delayed
- `9fd2ac56`, `c5b1f818`, `72a55bac`: lag 66–86 hours

**Key finding**: Both platforms have SOME perfect per-turn syncs and SOME long-delay outliers. The "Mac is broken, Windows is fine" hypothesis is **not supported by data**. Both platforms show intermittent failure.

## Identified Bug (partial fix)

`_sync_runner.py` previously had `import msvcrt` inside `log()`. `msvcrt` is a **Windows-only** stdlib module — on macOS/Linux it raises `ModuleNotFoundError`.

This means on Mac:
- Any `log()` call (success or error path) would raise `ImportError`
- `sync_one_file()` itself is called BEFORE the first `log()` in the success path, so the MongoDB upsert would still happen — but logging would silently fail
- In the file-not-found path, `log()` is called BEFORE `sync_one_file()`, so a missing file would result in zero MongoDB sync AND no log entry

**This bug is real and worth fixing**, but it does NOT fully explain the intermittent missing syncs because:
1. Some Mac sessions DO sync per-turn (0-minute lag in data above)
2. The msvcrt failure happens AFTER `sync_one_file()` in the common success path, so sync should still occur

## Fix (committed 2026-05-18)

`_sync_runner.py` now uses cross-platform locking:
- Windows → `msvcrt.locking()` (existing behavior)
- Mac/Linux → `fcntl.flock()` (new)

Helper functions `_acquire_lock()` / `_release_lock()` dispatch on `sys.platform == 'win32'`.

Also added a `CHILD STARTED` diagnostic line written at the very top of `main()` using a no-import, no-lock helper (`_diag_write`). This proves the child process actually spawned, distinguishing "Popen failed" from "child started but died".

## Open Hypotheses (after the partial fix)

The msvcrt bug is fixed but it likely isn't THE root cause of intermittent sync misses. Remaining candidates:

1. **`subprocess.Popen` with `start_new_session=True` (POSIX) sometimes fails to spawn the child** — would explain "Stop fired but nothing happened" with no log entry at all.
2. **MongoDB connection / network flake** — `get_db()` or `sync_one_file()` raises, outer `except` tries to log, dies silently. Will surface as `SYNC ERR` after the cross-platform log fix.
3. **JSONL file locked by Claude Code at the moment the child reads it** — would manifest as parse errors or partial reads.
4. **Race condition between consecutive Stop events** — fast successive turns could overlap, second process clobbers first.
5. **Hook simply not registered in `~/.claude/settings.json` on some installs** — would explain "no log entry ever".

## Verification Plan (next time on Mac)

```bash
cd ~/Projects/claude-toolkit && git pull          # pick up the fix + diagnostic
# do one Claude turn, then immediately:
tail -20 sync.log
cat ~/.claude/settings.json | grep -A 5 -i hook
```

Expected outcomes:

| sync.log result                                          | Diagnosis                                     |
| -------------------------------------------------------- | --------------------------------------------- |
| No new lines                                             | hook not registered, OR Popen never spawned   |
| `Stop -> ... sync started` only                          | parent fires but child fails to spawn         |
| `CHILD STARTED` only                                     | child runs, import or sync_one_file fails     |
| `CHILD STARTED` + `SYNC ERR <traceback>`                 | sync_one_file raised — traceback shows what   |
| `CHILD STARTED` + `SYNC OK` + lag < 5s                   | **working as designed**                       |

## TODO / Further Investigations

### Immediate (next time on Mac)
- [ ] **Mac verification run** — pull origin/main, do one turn, tail sync.log, classify outcome per the table above
- [ ] **Check Mac hook registration** — `cat ~/.claude/settings.json | jq '.hooks'` to confirm Stop hook points at `hook_sync.py`
- [ ] **Confirm Mac venv** — verify `~/Projects/claude-toolkit/.venv/bin/python` exists and has `pymongo` installed
- [ ] **Manual sync test** — run `_sync_runner.py --file <recent-jsonl> --sid test123` directly on Mac and verify both sync.log entry AND MongoDB update

### If verification shows Popen spawn failure
- [ ] Replace `start_new_session=True` with `posix_spawn` or `multiprocessing` to see if Popen detachment is platform-flaky
- [ ] Add stderr capture to `hook_sync.py` Popen call temporarily to surface spawn errors

### If verification shows sync_one_file failure
- [ ] Add per-step logging inside `sync_one_file` (file read, parse, get_db, upsert) to find which step raises
- [ ] Check MongoDB Atlas IP allowlist — Mac on different network than Windows?
- [ ] Confirm `.env` MONGODB_URI is identical between Mac and Windows installs

### Cross-platform robustness (regardless of root cause)
- [ ] Audit other scripts (`hook_sync.py`, `hook_notify.py`, `hook_session_guard.py`) for Windows-only imports or path assumptions
- [ ] Consider switching from detached-child model to a foreground sync with a short timeout — slower per-turn but eliminates entire class of detachment bugs
- [ ] Add a periodic "scan and reconcile" cron that catches anything the hook missed (separate from the per-turn path)

### Observability
- [ ] Send sync failures to MongoDB itself (a `sync_errors` collection) so we can see failure rates across machines without SSHing in
- [ ] Add `synced_via` field to session docs (`hook` vs `scan` vs `manual`) so we can quantify how often the hook actually works
- [ ] Add lag histogram to a daily summary so trends are visible

### Documentation
- [ ] Once root cause confirmed, write a one-pager in `INSTALL.md` for "verifying the hook is working" after a fresh install on a new machine
- [ ] Document the failure modes table in `README.md` under a "Troubleshooting" section

## Timeline
- 2026-04-06: Issue first observed in DentWebMigration session. Logging added to hook_sync.py.
- 2026-05-18: Empirical data review showed both Mac and Windows have intermittent sync misses (not Mac-only). msvcrt cross-platform bug fixed in `_sync_runner.py`. `CHILD STARTED` diagnostic added to distinguish failure modes. Root cause still TBD pending Mac verification.
