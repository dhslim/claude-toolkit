---
name: transplant
description: Clone a Claude Code session JSONL into another working directory as an independent copy
disable-model-invocation: true
argument-hint: <source.jsonl> <target-directory>
---

# Session Transplant

Clones a Claude Code session JSONL from one working directory into another, so the cloned session shows up in `claude -r` from the target directory as if it had been created there.

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_transplant.py $ARGUMENTS
```

**IMPORTANT**: The paths above are placeholders. During install, replace `{{VENV_PYTHON}}` and `{{SCRIPT_DIR}}` with the absolute paths to this repo's venv python and script directory.

## Arguments

Two positional arguments, in order:

1. **Source** — absolute path to the source session's `.jsonl` file under `~/.claude/projects/<encoded-cwd>/`
2. **Target directory** — absolute path to the working directory where you want the clone to appear in the resume picker

## Example

```
/transplant /c/Users/user/.claude/projects/C--Users-user-Desktop-old-project/abc123-....jsonl C:\Users\user\Desktop\new-project
```

## What it does

- Generates a brand-new `sessionId` UUID for the clone, so the original and the clone have zero identity overlap and can coexist
- Reads every line of the source JSONL
- Rewrites the per-line `cwd` field to the target directory
- Rewrites the per-line `gitBranch` field to match the convention used by sibling sessions in the target encoded project dir (falls back to `git branch --show-current` if no siblings exist)
- Rewrites the per-line `sessionId` field to the new UUID
- Flattens the first user message from list-form (e.g. pasted image + text) to a plain string — Claude Code's resume picker hides sessions whose first user message is non-string content from the default "current worktree" view
- Compacts the pre-user header: keeps only 1 `permission-mode` + 1 `file-history-snapshot` before the first user message, moves excess snapshots after it — the picker scans the first N lines to find the first user message and gives up if too many snapshots precede it
- Writes the clone to `~/.claude/projects/<encoded-target>/<newSessionId>.jsonl`
- Leaves the source file completely untouched

## What it does NOT do (intentional)

- Rewrite tool_result content strings (stale paths in transcript text are cosmetic)
- Touch `uuid`, `parentUuid`, `version`, or `timestamp` (message-level identity chain)
- Delete the source

## After running

The script prints a `Next:` line with the command to verify the transplant:

```
cd "<target>" && claude -r
```

Open the resume picker in the target directory and the cloned session should appear at or near the top with the same first user message as the source.
