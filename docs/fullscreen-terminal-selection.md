# Fullscreen terminal text selection (drag-select + copy)

## Symptom

In `tui: "fullscreen"` mode, drag-selecting text in the Claude Code terminal is
janky: selection stops working after each turn and only comes back after
**resizing the window**. Copying what you selected may also silently fail.

## Root cause

Fullscreen mode turns on **mouse reporting** so Claude can handle clicks, hover,
and scroll. Mouse reporting is a single channel: the terminal routes mouse events
to *exactly one* consumer — either the app (Claude) or the terminal's own native
text selection, never both at once. So you cannot have plain drag-select **and**
Claude's in-app mouse simultaneously; that is terminal behaviour, not a Claude
limit (tmux, vim, htop all behave the same).

"Resizing fixes it" because a real window resize fires **SIGWINCH**, and Claude's
resize handler re-asserts the terminal state — briefly restoring native selection
— until the next redraw re-captures the mouse.

## Dead ends (don't bother)

- **`Ctrl+L`** — only repaints the screen; it does not re-run Claude's terminal
  setup, so it never re-emits the mouse mode. No effect on selection.
- **`kill -WINCH <pid>`** — a bare SIGWINCH with no actual size change: Claude
  sees "same size" and no-ops. Only a real dimension change re-inits the mouse.
  (Forcing a real per-turn resize via a hook would flash a redraw and spam
  scrollback — not worth it.)
- **`CLAUDE_CODE_DISABLE_MOUSE_CLICKS=1`** — keeps mouse reporting on (to preserve
  wheel scroll), so native drag-select is *still* blocked, and it also disables
  in-app clicks. Net worse: no in-app mouse **and** no selection.

## The fix (keeps everything: fullscreen + in-app mouse + selection)

Use the terminal's **modifier-key override** for native selection. In VS Code's
integrated terminal, add to VS Code **user** `settings.json`
(macOS: `~/Library/Application Support/Code/User/settings.json`):

```jsonc
"terminal.integrated.macOptionClickForcesSelection": true,
"terminal.integrated.copyOnSelection": true,
```

Then:

- **Hold Option (⌥) and drag** to select — bypasses mouse reporting, so it works
  every turn with no resize.
- `copyOnSelection` copies the instant you release (mouse-up), before any redraw
  can clear it, so just **Cmd+V** to paste. Do **not** use Cmd+C: with no
  *normal* selection it sends SIGINT, which is why copy appears to fail.
- The ⌥-selection highlight renders in a lighter "override" shade. Cosmetic — the
  copied text is identical.

On an external **PC keyboard**, **Alt = Option** by default (⊞ Windows = Command),
so hold **Alt** to drag-select. Check a key's modifier via System Settings →
Keyboard → Keyboard Shortcuts → Modifier Keys, or run `cat -v` and press the key
plus a letter (⌥+a emits `å`).

These are **VS Code** settings (per-user, carried by Settings Sync), *not* the
toolkit's `~/.claude/settings.json`. The same keys apply on Windows/Linux VS Code
— there `macOptionClickForcesSelection` is unneeded; `Alt`-drag + `copyOnSelection`
is enough.

## Heavier alternatives (if you refuse to hold a modifier)

Both need a relaunch and cost something; the modifier approach above loses
nothing, so prefer it.

| Option | Gains | Loses | Notes |
|---|---|---|---|
| `CLAUDE_CODE_DISABLE_MOUSE=1` in `~/.claude/settings.json` `env` | plain drag-select + native scroll | **all in-app mouse** (click menus, click-to-expand, hover — keyboard still does these) | keeps fullscreen; restart to apply |
| drop `tui: "fullscreen"` | native mouse everything | pane no longer fills; a **blank strip** returns below the prompt (where background-task / workflow rows render) | restart to apply |

## One-line reason it's inherent

A terminal has one mouse-event channel. In fullscreen Claude owns it; to select
text natively you either hand the channel back entirely (lose in-app mouse) or
borrow it per-drag with a modifier key. The modifier is the only "keep both" path.
