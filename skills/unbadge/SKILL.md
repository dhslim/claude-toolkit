---
name: unbadge
description: Remove a Claude Code session's name, clearing the cyan session-name badge above the input box (the inverse of /rename)
disable-model-invocation: true
argument-hint: "[session-id | blank to list]"
---

# Clear a session's name badge

`/rename` has no inverse. This reverts it. The name lives in **three** stores with
three lifetimes — missing the third is why the badge comes back.

The script is `{{SCRIPT_DIR}}/session_unname.py`. Use the venv python:
`{{VENV_PYTHON}}`

## Step 1 — no argument given: list candidates

Run this and show the table, then ask which session to clear:

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_unname.py --list
```

`jobs YES` means daemon-backed (three stores). Stop here and wait for the user to pick.

## Step 2 — a session was given: analyze first, never act blind

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_unname.py $ARGUMENTS --analyze
```

Relay: how many name entries, whether it is 2-store or 3-store (daemon-backed), and
whether any live pid holds it.

**If the report says the session is LIVE, stop.** Tell the user to `/exit` that
session first and explain why: a named session re-stamps the transcript **every
turn**, so a strip performed against a live process lasts seconds. Do not pass
`--force` to work around this unless the user explicitly insists.

**Never target the session you are currently running in** — it is live by definition.

## Step 3 — do the revert (only after the user confirms)

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_unname.py $ARGUMENTS --yes
```

It backs up both files (`.bak-unname-<timestamp>`) before touching them, strips the
transcript's `custom-title` / `agent-name` entries, and removes **only** `name` and
`nameSource` from job state (all other keys preserved). It prints an after-state
verification.

## Step 4 — tell the user how to bring it back

```
claude --resume <full-session-id>
```

**Not `--continue`** — that picks the most recent session in the folder and may grab
the wrong one. On resume the session record is recreated with an auto-derived name
(`<slug>-<xx>`, `nameSource: "derived"`), which is exactly the no-badge state.

## Push back before doing this

The badge is cosmetic; **the name is not.** It is injected into the model's context
each turn as a hint about the session's purpose. If the user's real complaint is that
the name is *wrong*, say so and recommend `/rename <something-truthful>` instead —
one command, no file surgery, and it improves the thing that actually matters. Only
proceed when they genuinely want *no* name.

## Caveats to state plainly

- The scrollback still contains the literal `/rename` line the user typed. The rename
  stays visible in history; it is simply no longer in effect.
- The tab title may still show a name — the badge and the tab are **different
  consumers** reading the name by different rules. Clearing the badge does not
  necessarily blank the tab.
- Counting these entries with `grep` **over-counts**: any message that merely
  discusses `custom-title` / `agent-name` matches. The script JSON-parses each line
  and compares `type` exactly; trust its numbers over a grep.

Full investigation: `docs/session-badge-removal.html`
