# Version drift & provenance

How claude-toolkit tracks its own version across machines: which commit a machine
is on, when it's behind, and which version produced each session. There are no
version numbers — the git commit **is** the version (commit-as-version).

## The pieces

| Concept | Implementation |
|---|---|
| source of truth ("latest") | `origin/main` on GitHub |
| a machine's installed version | its local `HEAD` (commit SHA) |
| "am I behind?" | `git rev-list --count HEAD..origin/main` |
| the changelog | `git log HEAD..origin/main` |
| apply an update | `git pull` (never automatic) |
| "you're behind" notice | `hook_toolkit_drift.py` (SessionStart hook) |
| version stamped on output | `toolkit_commit` on each session doc |

Everything is **per-machine**: each machine has its own clone at its own `HEAD`,
pulls on its own schedule, and runs its own hooks against its own local state.
The only shared thing is `origin/main`.

## How "N behind" is computed

Git history is a chain of commits, each pointing at its parent. Two pointers matter:
`HEAD` (this machine's commit) and `origin/main` (where GitHub's main was **at the
last fetch**). "Behind" is the set of commits `origin/main` has that `HEAD` doesn't:

```
git rev-list --count HEAD..origin/main
```

`HEAD..origin/main` = "everything reachable from origin/main but NOT from HEAD".
This is **pure local graph math — instant, offline**. It never talks to GitHub.

The only step that needs the network is `git fetch`, which moves the `origin/main`
pointer to GitHub's current tip. So the two halves have very different costs:

- **fetch** — slow, network, can fail offline. Updates the snapshot.
- **count** — instant, local, always available. Reads the snapshot.

A machine that makes and pushes a commit can never be "behind" it: committing moves
that machine's `HEAD` forward in the same act, and the push moves `origin/main` to
match. Being behind means `origin/main` moved but *your* `HEAD` didn't — which only
happens to machines that **didn't** push.

## The drift notifier (`hook_toolkit_drift.py`)

A SessionStart hook. Because a session start must never lag, it does **not** fetch
on the hot path — it reads a cached count and refreshes in the background.

1. Read the cache `~/.claude/toolkit_drift.json` → `{"behind": N, "ts": <unix>}`.
2. If `N > 0`: inject `hookSpecificOutput.additionalContext` phrased as an
   instruction ("toolkit is N behind — tell the user to `git pull`"), plus the
   incoming `git log` as the changelog.
3. If the cache is missing or **stale** (older than the TTL), spawn a **detached**
   `--refresh` (git fetch + recount + rewrite cache) that runs off the hot path.

Notify-only — it never pulls. Silent when `N == 0` (that's it working). Opt out
with `TOOLKIT_NO_UPDATE_CHECK=1`. First run stays silent (nothing cached yet).

### The TTL — there is no central clock

`TTL_SECONDS = 24 * 60 * 60` (24h). **Nothing counts down.** There is no daemon,
cron, or timer holding a 24-hour clock. The TTL is a **lazy timestamp comparison**
done only when a session starts:

```python
if not cache or (now - cache["ts"]) > TTL_SECONDS:
    spawn_refresh()
```

- `cache["ts"]` is a reading of the clock taken **when the cache was last written**
  (`time.time()` — Unix epoch seconds, UTC-based).
- `now` is a fresh reading of the same clock at session start.
- The "timer" is reconstructed each time by subtracting the two. It isn't stored
  or held anywhere.

The "central clock" is therefore just **each machine's own OS wall clock**
(`time.time()`), and the mechanism is **event-driven**: if no session starts for a
week, nothing refreshes — nothing is watching. The next session start does the
subtraction, sees `> 24h`, and refreshes.

### The timeliness trade-off

Because the hook reads the cache *now* and refreshes for *next time*, a behind
machine can be up to **~24h + one session** late to notice:

- session A finds the cache stale → shows the (possibly old) count, spawns a refresh
- the refresh writes the fresh count
- session B reads the fresh count → shows the accurate "behind"

For a toolkit updated many times a day this is the main imperfection. Shrinking
`TTL_SECONDS`, or switching the stale case to a short **blocking** fetch
(`fetch-on-stale`), trades a rare ~1s startup blip for near-immediate detection.

## Session provenance (`sync_conversations.py`)

Every synced session records the toolkit version it ran under. `toolkit_commit_info()`
reads the repo's `HEAD` once per sync process and `sync_one_file()` stamps three
fields onto the session document:

```
toolkit_commit       short SHA of HEAD          e.g. "a4b9934"
toolkit_commit_date  committer ISO-8601 date
toolkit_dirty        uncommitted changes present?
```

This runs on the normal sync path too: `hook_sync.py` (Stop / SessionEnd) spawns
`_sync_runner.py --file`, which calls `sync_conversations.sync_one_file` — so the
stamp is applied automatically, not only on a manual `--file`/`--scan`.

Because the session doc already carries `device`, a session is tagged with both
*which machine* and *which toolkit version* produced it — enough to correlate
behavior to versions or watch a version roll out across machines over time.
