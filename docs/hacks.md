# Claude Code Hacks

## Session Import Between Projects

Claude Code doesn't have a built-in way to import a session from one project into another. But you can do it manually:

**Steps:**
1. Find the session JSONL file in `~/.claude/projects/<source-project>/<session-id>.jsonl`
2. Copy the JSONL file to the target project directory:
   ```bash
   cp ~/.claude/projects/<source-project>/<session-id>.jsonl \
      ~/.claude/projects/<target-project>/
   ```
3. Also copy the session folder (artifacts) if it exists:
   ```bash
   cp -r ~/.claude/projects/<source-project>/<session-id> \
         ~/.claude/projects/<target-project>/
   ```
4. From the target project directory, run `claude --resume` and the session will appear in the session picker.

**Alternative:** Resume directly by session ID:
```bash
cd /target/project && claude --resume <session-id>
```

**Why:** Useful when a session was started in a generic directory (e.g., `/root`) but the context is relevant to a specific project.
