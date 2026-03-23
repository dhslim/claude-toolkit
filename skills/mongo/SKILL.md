---
name: mongo
description: Query recent Claude Code activity across all machines from MongoDB
disable-model-invocation: true
argument-hint: <duration> (e.g. 10, 30m, 2h, 1d — default unit is minutes)
---

# Recent Activity Query

**ALWAYS run the command below as a background task** (using `run_in_background`). The user expects to type their follow-up prompt immediately without waiting. Present results when the background task completes.

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/mongo_recent.py $ARGUMENTS
```

**IMPORTANT**: The paths above are placeholders. During install, replace `{{VENV_PYTHON}}` and `{{SCRIPT_DIR}}` with the absolute paths to this repo's venv python and script directory.

The script returns session data from the specified time window across all machines.

## How to present results

Summarize the output concisely:
- Group by session (use session name if available, otherwise session ID prefix)
- Show device/machine name, project path
- For each session, summarize what was discussed/worked on based on user and assistant messages
- Highlight key topics: bugs fixed, features built, decisions made, tools used
- Keep it scannable — bullet points, not paragraphs
- If there are many sessions, organize by device or project
