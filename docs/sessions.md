# Claude Code Sessions — Storage, JSONL Format, and Transplanting

A reference for how Claude Code stores sessions on disk, how the resume picker decides what to show, and how to safely clone a session from one working directory into another.

> **Status:** This doc captures findings from a deep investigation into Claude Code's session internals. Things marked **CONFIRMED** were verified empirically or against official docs. Things marked **UNVERIFIED** or **OPEN QUESTION** are working hypotheses we couldn't pin down.

## TL;DR

- A session is a single `.jsonl` file at `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`
- `<encoded-cwd>` is the absolute path with `\` and `/` replaced by `-` and the drive colon replaced by `--` (Windows). Reversible, not hashed.
- Every line in the JSONL has a `cwd` and `sessionId` field. The resume picker checks **both** the encoded directory name **and** the per-line `cwd` field — if they disagree, the session is hidden.
- To transplant a session into a new working directory you must (1) write the file under the correct new encoded directory and (2) rewrite the per-line `cwd` field everywhere. The script `session_transplant.py` does this end-to-end.
- The picker has a non-obvious filter: **the first user message's content must be a plain string, not a list**. Sessions whose first user message includes an image paste (list-form content) are hidden from the default "current worktree" view and only appear under Ctrl+W "show all worktrees".

## Storage layout

### The project directory

```
~/.claude/projects/
├── C--Users-user-Desktop-foo/
│   ├── 47fff34f-ef4e-42e1-8372-e2ef52418111.jsonl    ← a session
│   ├── 47fff34f-ef4e-42e1-8372-e2ef52418111/         ← optional sidechain dir
│   │   └── subagents/
│   │       └── agent-XXXX.jsonl
│   └── memory/
│       └── MEMORY.md
├── C--Users-user-Desktop-bar/
│   └── ...
```

Each top-level directory under `~/.claude/projects/` represents a single working directory the user has run Claude from. Inside it:

- One `.jsonl` file per session (filename is the session UUID)
- Optionally a directory of the same name (without `.jsonl`) containing per-session sidechain data — subagent transcripts, etc.
- A `memory/` directory holding auto-saved memory entries for that working directory

### Encoded directory name

The directory name is the absolute path with separators flattened to dashes:

| Original path | Encoded directory name |
|---|---|
| `C:\Users\user\Desktop\foo` | `C--Users-user-Desktop-foo` |
| `/home/user/foo` | `-home-user-foo` |
| `/Users/alice/projects/bar` | `-Users-alice-projects-bar` |

Encoding rules:
- All `\` and `/` characters → `-`
- On Windows, the first `:` after the drive letter → `-` (so `C:` becomes `C-`, then the immediately following `\` also becomes `-`, yielding `C--`)
- No hashing — fully reversible

### Adjacent state files

Some other files Claude Code maintains globally:

- **`~/.claude.json`** — global state. Contains a `projects` dict keyed by absolute path with per-project metadata (`lastSessionId`, `lastSessionMetrics`, MCP server config, etc.). Note: not every encoded directory under `~/.claude/projects/` necessarily has an entry here. Worktrees you've never run Claude from interactively may be missing from this dict but still have JSONL files on disk.
- **`~/.claude/sessions/<pid>.json`** — per-process liveness markers. One file per running Claude Code process, recording PID, sessionId, cwd, startedAt. Used to detect concurrent sessions.
- **`~/.claude/todos/<sessionId>-agent-*.json`** — todo lists associated with sessions and agents.
- **`~/.claude/projects/<encoded-cwd>/memory/`** — auto-memory for that working directory (the system that produces the `MEMORY.md` index plus individual memory files).

## JSONL anatomy

Each line in a session JSONL is a single JSON object representing one event in the conversation. The file is append-only — Claude Code adds to it as the conversation progresses.

### Line types

| `type` | What it represents |
|---|---|
| `permission-mode` | The session's permission mode (default mode, bypassPermissions, etc.). Usually the first line of the file. |
| `file-history-snapshot` | A snapshot of which tracked files Claude is monitoring, with hashes. Used for the rewind/undo feature. |
| `user` | A user message. Contains the message content, may have attachments. |
| `attachment` | A file or image attached to a user message. Tracked separately from the user message itself. |
| `assistant` | An assistant message. Contains text, tool_use blocks, thinking blocks, etc. |
| `system` | System-generated messages (hooks output, turn duration, stop hook summary, etc.). Has a `subtype` field for the specific kind. |
| `progress` | Progress events from hooks during a turn. |
| `queue-operation` | Internal queue management (rare). |

### Key fields per line

Most non-trivial lines (`user`, `assistant`, attachment, system) have the following structure:

```json
{
  "parentUuid": "...",        // uuid of the previous message in the chain (null on first)
  "isSidechain": false,       // false = main conversation, true = subagent
  "type": "user",             // line type
  "message": { ... },         // the actual content
  "uuid": "...",              // unique id of this message
  "timestamp": "ISO-8601",    // when it happened
  "permissionMode": "bypassPermissions",
  "userType": "external",
  "entrypoint": "cli",
  "cwd": "C:\\Users\\user\\Desktop\\foo",  // ← LOAD-BEARING
  "sessionId": "47fff34f-...",             // ← LOAD-BEARING
  "version": "2.1.97",                     // Claude Code version at time of write
  "gitBranch": "HEAD"                      // see "branch detection" below
}
```

The first two lines of a fresh session are usually special:

- Line 0: `{"type": "permission-mode", "permissionMode": "...", "sessionId": "..."}`
- Line 1: `{"type": "file-history-snapshot", "messageId": "...", "snapshot": {...}}`

Neither of these has a `cwd` or `gitBranch` field — those start appearing on the user/assistant lines from line 2 onward.

### Identity vs location fields

When transplanting, it's critical to know which fields are **identity** (should never change) versus **location** (must change to match the new working directory):

| Field | Category | Notes |
|---|---|---|
| `sessionId` | Identity (with caveat) | Should be globally unique. When *cloning* a session for true independence, generate a new UUID. When merely *moving* a session, keep it. |
| `uuid` | Identity | Per-message, never change |
| `parentUuid` | Identity | Forms the message tree, never change |
| `timestamp` | Identity | Snapshot of when the message happened, never change |
| `version` | Identity | Snapshot of which Claude Code version wrote it, never change |
| **`cwd`** | **Location** | **MUST be rewritten when transplanting** |
| **`gitBranch`** | **Location** | **Should be rewritten** to match the target worktree convention |
| `userType`, `entrypoint`, `permissionMode` | Identity-ish | Don't typically need to change |

## The dual binding

**The key insight:** The resume picker uses **both** the filesystem location of the JSONL **and** the per-line `cwd` field to decide whether a session belongs to a working directory.

```
~/.claude/projects/C--Users-user-Desktop-foo/abc123-...jsonl
                   └── must match ──┐
                                    ▼
{ ..., "cwd": "C:\\Users\\user\\Desktop\\foo", ... }
```

If the encoded directory name says one thing and the per-line `cwd` field says another, the picker will hide the session — even when run from the directory the file is physically in.

This is the gotcha that makes naive `cp` of a JSONL into a different project directory fail: the file shows up on disk in the new location, but the picker silently filters it out because the per-line `cwd` still references the old path.

**To make a transplant work, both must be consistent.**

## The resume picker filter — confirmed and unverified

When you run `claude -r` in a directory, the resume picker shows entries from `~/.claude/projects/<encoded-current-cwd>/`. By default it filters to "current worktree" — but the filter has additional rules beyond just file location.

### CONFIRMED: First user message must be plain string

The picker hides sessions from the **default current-worktree view** when the first user message has **list-form content** (e.g. `[{"type": "text", "text": "..."}, {"type": "image", ...}]` — what gets produced when the user starts a session by pasting an image).

Such sessions only appear after pressing **Ctrl+W** (show all worktrees), never in the default view.

**The fix:** flatten the first user message's content from list to plain string by joining the text parts. This is what `session_transplant.py` does automatically with `flatten_first_user_message()`.

This was discovered empirically: a freshly-cloned session was invisible in the default view until we rewrote the first user message's content. After flattening, it appeared at the top of the picker.

### CONFIRMED: Per-worktree branch convention

Different worktrees use different `gitBranch` value conventions. Some write the literal branch name (`feature/dhslim-#1747`). Others write the literal string `"HEAD"`. The script's `sibling_git_branch()` function detects the local convention by reading existing JSONL files in the target encoded directory and using the most common value.

**Why the difference exists:** UNVERIFIED. Possibly related to special characters in branch names (`#`), git's detached HEAD behavior, or differences in Claude Code's internal git detection across versions.

### UNVERIFIED: Other suspected filters

Several sessions in our investigation remained hidden from the default picker even after fixing first user message + branch convention. We could not pin down why. Possibilities (none verified):

- **Mixed `gitBranch` values within the same file** — some files have both `"HEAD"` and a branch name on different lines, possibly because Claude's branch detection changed mid-session.
- **First message age** — sessions with `first_msg_ts` older than ~7 days may be filtered.
- **First-message dedup** — sessions whose first user message text is identical to another (more recent) session may be hidden as the older duplicate.

If you transplant a session and it appears under Ctrl+W but not in the default view, the most likely culprit is one of the above — but we don't have a definitive answer. **Open question.**

### Hard ~250-line cap on the picker render buffer

Independent of all the above filters, Claude Code's TUI has an internal cap of approximately **250 lines** of session history visible in the picker render buffer at once, regardless of your terminal's scrollback configuration. See:
- [anthropics/claude-code#40253](https://github.com/anthropics/claude-code/issues/40253) (closed as duplicate of #28077)
- [anthropics/claude-code#28077](https://github.com/anthropics/claude-code/issues/28077) (open, the canonical tracking issue)

## Branch detection quirks

Claude Code writes a `gitBranch` field on every non-trivial JSONL line. The value is determined by Claude's internal git detection at the moment the line is written. This produces some surprising behavior:

- **Same worktree, mixed values within one file:** If the worktree's git state changes mid-session (rebase, switch, detach), the gitBranch values written from that point on differ from earlier ones. We saw this on a real session in the family-1830 worktree: lines 25–2154 had `"fix/dhslim-#1830"`, lines 2162–2512 had `"HEAD"`.

- **Unrelated worktrees use different conventions:** Some worktrees consistently use the branch name, some consistently use `"HEAD"`. Across our test setup we found both styles in different worktrees of the same parent repo. The reason is unknown.

- **Non-git directories:** A directory that isn't a git repo (e.g. `C:\Users\user`) writes `"HEAD"` as the gitBranch on every line. Effectively, `"HEAD"` is the fallback when there's no real branch to report.

**Practical implication for transplanting:** Don't naively use `git branch --show-current` to derive the target gitBranch. Instead, look at sibling sessions in the target encoded directory and use whatever value they use. `session_transplant.py` does this via `sibling_git_branch()`.

## Transplanting a session

### What MUST be rewritten

To make a session findable in a new working directory:

1. **File location.** The `.jsonl` must be physically present under `~/.claude/projects/<encoded-target-cwd>/`. Filename is the session UUID `.jsonl`.
2. **Per-line `cwd`** — every line that has a `cwd` field must point at the new absolute path.

That's the absolute minimum for the file to even be recognized.

### What SHOULD be rewritten

To make the cloned session a clean, fully independent copy that the picker happily displays in the default view:

3. **`sessionId`** — generate a fresh UUID and rewrite it in every line that has `sessionId`. This guarantees the original and clone have no identity overlap and can coexist.
4. **`gitBranch`** — rewrite every line's `gitBranch` to match the convention used by sibling sessions in the target directory (or fall back to `git branch --show-current` if there are no siblings).
5. **First user message content** — if the first user message's content is a list (e.g. text + image), flatten it to a plain joined-text string. Otherwise the picker hides the session from the default view.

### What's cosmetic (don't bother)

The following are visible in the rendered transcript text but don't affect picker behavior or session loading:

- **Tool result content strings** containing absolute paths to the old project directory (e.g. `C:\Users\user\Desktop\old\src\foo.ts`) — these stay as historical text and look out of place but break nothing.
- **Image cache paths** referencing the old `image-cache/<old-sessionId>/N.png`.
- **Embedded references** to the old sessionId inside hook output paths or task notification metadata (`/AppData/Local/Temp/claude/.../<old-sessionId>/tasks/...`).

`session_transplant.py` intentionally does NOT rewrite these because (a) they're inside arbitrary tool result text where blanket find-and-replace risks corrupting unrelated content, and (b) they don't affect functionality.

### What MUST NOT be touched

- `uuid`, `parentUuid` — message-level identity. Changing them breaks the message tree.
- `timestamp` — historical record.
- `version` — snapshot of which Claude Code version wrote each message; future versions may behave differently if they see a wrong number here.

### The script

`session_transplant.py` (top-level in this repo) does all of the above safely:

```bash
python session_transplant.py <source.jsonl> <target-directory>
```

Or via the slash command if you've installed the `/transplant` skill:

```
/transplant <source.jsonl> <target-directory>
```

The script:
- Generates a fresh sessionId UUID
- Rewrites cwd, gitBranch (sibling-aware), and sessionId on every line
- Flattens the first user message if it's list-form content
- Writes to `~/.claude/projects/<encoded-target>/<newSessionId>.jsonl`
- **Leaves the source file completely untouched** (non-destructive copy)

See the script's docstring and `skills/transplant/SKILL.md` for full details.

## Open questions

These came up during the investigation and we couldn't resolve them:

1. **Why some sessions stay hidden from the default picker even with consistent fields.** The 7bace25c session in our test setup had a string first user message, was the right cwd, was in the right encoded dir, and still wouldn't appear in the default view. Suspected causes: mixed gitBranch values, first-message age, or a dedup filter against newer sessions with identical first messages. None confirmed.

2. **What determines whether Claude writes `"HEAD"` vs the branch name to the gitBranch field.** It varies by worktree and seemingly by version. Not documented anywhere we could find.

3. **The exact filter logic of the picker** — there is no official documentation of the filter rules. Everything we know was reverse-engineered by observing which sessions appeared and which didn't.

4. **Whether the per-session sidechain directory** (`<sessionId>/subagents/`) needs to be transplanted alongside the main JSONL. Our script doesn't currently copy it. Sessions transplanted without their sidechain directory worked correctly in testing, but if the source had subagent runs, those won't be visible from the clone.

Contributions / corrections welcome.

## See also

- [`docs/hacks.md`](hacks.md) — older manual approach to session import (predates `session_transplant.py`)
- [`docs/scrollback.md`](scrollback.md) — Claude Code rendering, fullscreen mode, scrollback, and the `Ctrl+O → [` history dump trick
- `session_transplant.py` — the script
- `skills/transplant/SKILL.md` — `/transplant` slash command
