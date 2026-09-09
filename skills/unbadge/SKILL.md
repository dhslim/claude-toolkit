---
name: unbadge
description: Guided workflow to remove Claude Code session names and clear the cyan session-name badge — reports what is named, which live processes hold them, gates on verified shutdown, clears, then hands back resume commands
disable-model-invocation: true
argument-hint: "[session-id(s) | blank for the full report]"
---

# Unbadge — guided session-name removal

`/rename` has no inverse. This is the gated process that reverts it safely.

Script: `{{SCRIPT_DIR}}/session_unname.py` · python: `{{VENV_PYTHON}}`

You are driving a **5-stage pipeline with a hard gate in the middle**. Do not skip
stages and do not clear anything until the gate passes. Stage 4 is the only
destructive step.

---

## Stage 1 + 2 — report what is named, and what is holding it

Run this first, always. With no argument it covers every named session; with
argument(s) it narrows to those:

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_unname.py --plan $ARGUMENTS
```

It prints five sections. Relay stages 1 and 2 to the user in your own words:

- **Which sessions carry an explicit name**, how many transcript entries each has,
  whether it is **2-store or 3-store** (`3 daemon` = daemon-backed, needs job state
  cleaned too, and is the case that defeats manual attempts), and its live status.
- **Which live processes hold them** — each with pid, image, the **badge text**, the
  **cwd** (so the user can find the window), and its state.

Point out anything notable: names that are *descriptive* (and therefore doing useful
work) versus obvious junk, and any session held by a non-interactive/daemon process.

## Stage 3 — tell the user exactly what to exit, then STOP

If stage 2 lists live holders, relay the per-session ACTION lines verbatim in spirit:

- **interactive** → switch to that window (identified by cwd + badge text) and type
  `/exit`.
- **not interactive** (background/daemon job) → stopping it is different; do not tell
  them to `/exit` a terminal that isn't there. Point them at `claude agents`.
- **`NOTE image is not claude`** → the pid exists but belongs to another program, so
  it is almost certainly a **recycled pid** and that session is likely already dead.
  Say so; it will clear itself on the next verify.

Then **stop and wait.** Do not proceed to the gate on your own initiative. Exiting is
the user's action to take, in their own terminals.

**Never target the session you are currently running in** — it is live by definition,
and it re-stamps its own name every turn.

## Stage 4a — THE GATE (mandatory, re-run until it passes)

After the user says they've exited, verify by process rather than by trust:

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_unname.py --verify $ARGUMENTS
```

Read the **exit code**, not the prose:

| exit | meaning | what you do |
|---|---|---|
| **0** | every target provably dead | proceed to stage 4b |
| **1** | a live process still holds one | show which, go back to stage 3 |
| **2** | liveness UNKNOWN — process probe failed, or a session record is unreadable | **do not clear anything**; report why and stop |

Exit 2 is a fail-closed result, not a glitch: an unparseable session record could
belong to a live session, so "dead" cannot be trusted. Resolve it first.

## Stage 4b — clear (only on exit 0)

One session at a time:

```
{{VENV_PYTHON}} {{SCRIPT_DIR}}/session_unname.py <session-id> --yes
```

It backs up every file first (`.bak-unname-<timestamp>`), strips the transcript's
`custom-title` / `agent-name` entries, removes **only** `name` and `nameSource` from
job state (all other keys preserved), and prints an after-state verification. Relay
that verification — do not just say "done".

## Stage 5 — hand back the restart commands

```
claude --resume <full-session-id>
```

**Not `--continue`** — that picks the most recent session in the folder and may grab
the wrong one. The badge does not vanish the moment the files change: it is drawn from
state loaded at startup, so it clears on this fresh load. On resume the record is
recreated with an auto-derived name (`<slug>-<xx>`, `nameSource: "derived"`) — exactly
the no-badge state.

---

## Push back before any of this

The badge is cosmetic; **the name is not.** It is injected into the model's context
each turn as a hint about the session's purpose. If the user's real complaint is that
a name is *wrong*, recommend `/rename <something-truthful>` instead: one command,
applies live with no exit, no file surgery, and it improves the thing that actually
matters. Reserve this pipeline for "I want **no** name."

## Caveats to state plainly

- Scrollback still shows the literal `/rename` line the user typed. The rename stays
  visible in history; it is simply no longer in effect.
- The badge and the tab title are **different consumers** of the name. Clearing the
  badge may leave the tab title in place — often the desirable end state.
- `grep` **over-counts** these entries: any message that merely discusses
  `custom-title` / `agent-name` matches. The script JSON-parses each line and compares
  `type` exactly — trust its numbers over a grep.
- Store count is set by **origin, not usage**: a session born as a background job
  keeps job state forever, even after a terminal is attached and it is used as an
  ordinary chat for days.

Full mechanism: `docs/session-badge-removal.html`
