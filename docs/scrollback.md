# Claude Code Rendering, Scrollback, and Terminal Setup

How Claude Code renders to the terminal, why scrolling back through old conversation history is harder than it should be, and the workflow that actually lets you see the entire conversation in your terminal.

> **Status:** Verified against official Claude Code docs and observed behavior. Open issues at the bottom.

## TL;DR

- **For just reading old content in the conversation: press `Ctrl+Home`** in Claude Code's default view. Jumps to the very first message. `Ctrl+End` returns to the live position. `Page Up`/`Page Down` scroll one page at a time. This is the simple, default answer for 95% of "scroll back" cases.
- Since v2.1.89, Claude Code renders into the terminal's **alternate screen buffer** (like `vim` or `htop`). The alt buffer has **no scrollback**, so your terminal's native scroll-up doesn't reach Claude content directly. But `Ctrl+Home` works *within* Claude's viewport, so this usually doesn't matter.
- This is governed by the env var `CLAUDE_CODE_NO_FLICKER`. Counterintuitively, **`=1` enables fullscreen mode** (the default since v2.1.89), `=0` disables it.
- **For the niche case where you want the content in your terminal's native scrollback buffer** (so you can mouse-scroll across it, use terminal `Cmd+F` search, copy-paste large regions, etc.), use the in-app trick: **`Ctrl+O` → `[`** while in Claude Code. This dumps the full transcript into your normal buffer where standard scroll-up works. Two keystrokes. Power-user move, not the default.
- VS Code's `terminal.integrated.scrollback` setting only matters for the dump-trick path; raising it has no effect on what `Ctrl+Home` shows you (which uses Claude's internal viewport, not terminal scrollback).

## The architecture

### Two terminal buffers

Every modern terminal — including VS Code's xterm.js-based integrated terminal — has **two screen buffers**:

| Buffer | Contains | Scrollback? |
|---|---|---|
| **Normal buffer** | What you usually see — shell prompts, command output, typed input | ✅ Yes — every line that scrolls off the visible area enters scrollback and is reachable by scroll-up |
| **Alternate (alt) buffer** | A fixed-size screen for full-screen TUIs like `vim`, `htop`, `less` | ❌ No — when an app exits the alt buffer, the terminal restores the normal buffer state from before the app started; everything drawn in the alt buffer is gone |

When an app enters the alt buffer (escape sequence `\033[?1049h`), the terminal **saves** the normal buffer state, switches to the alt buffer, and lets the app draw whatever it wants in a sandboxed area. When the app exits (escape sequence `\033[?1049l`), the terminal restores the normal buffer exactly as it was — your shell history is intact, but everything the app drew is gone.

### What Claude Code does

**Since v2.1.89**, Claude Code's default behavior is to render in the **alt buffer**, controlled by the env var `CLAUDE_CODE_NO_FLICKER`. This is officially called "fullscreen rendering" and provides:
- Flicker-free, fixed-position input box at the bottom
- Faster redraws (Claude only repaints visible regions)
- A consistent layout regardless of terminal scroll position

The cost: **conversation history bypasses your terminal's scrollback entirely.** Scrolling up in the terminal shows you whatever was on screen *before* you ran `claude` — not the conversation.

### The env var (counterintuitive naming)

```json
// in ~/.claude/settings.json
{
  "env": {
    "CLAUDE_CODE_NO_FLICKER": "1"
  }
}
```

| Value | Mode | Scrollback for Claude content |
|---|---|---|
| `"1"` (default since v2.1.89) | **Fullscreen** (alt buffer) | ❌ Bypassed — must use `Ctrl+O → [` to access history |
| `"0"` | Pre-v2.1.89 normal mode | ✅ Native scrollback works for new content rendered live, but resumed sessions still don't paint full history |

The naming is misleading: `=1` is "no flicker enabled" — i.e. fullscreen mode. `=0` disables the flicker-free optimization and reverts to normal-buffer rendering.

### Why neither setting alone solves it

| Mode | Fresh sessions | Resumed sessions |
|---|---|---|
| `NO_FLICKER=0` | ✅ Native scrollback works as content arrives | ❌ `claude -r` doesn't paint history; you only get content from this process forward |
| `NO_FLICKER=1` | ❌ Alt buffer bypasses scrollback | ❌ Same — and `claude -r` still doesn't paint history |

So plain scroll-up never reaches the full conversation, no matter which mode you're in. You need a deliberate action to dump history somewhere scrollable.

## The simple answer: navigate within Claude's viewport

**Claude Code keeps the entire conversation in memory and lets you scroll through it with standard navigation keys, even though those messages aren't in your terminal's scrollback.** This is the default and easiest answer for "I want to look back at what was said earlier."

| Key | Action |
|---|---|
| **`Ctrl+Home`** | **Jump to the very first message** of the conversation |
| **`Ctrl+End`** | Jump back to the bottom (the live prompt position) |
| `Page Up` / `Page Down` | Scroll one page up/down through the conversation |
| Up/Down arrows (when viewport-scrolled) | Scroll line by line |

You're scrolling Claude's TUI viewport — **not** the terminal scrollback. The full conversation is loaded into Claude's memory and the TUI walks through it. This works regardless of `CLAUDE_CODE_NO_FLICKER` setting and regardless of your terminal scrollback configuration.

### When this is enough

- You want to remember what you discussed earlier
- You want to find a specific code block or message you sent earlier
- You want to check what tools were called

### When it's NOT enough

- You want to **search the entire conversation** with the terminal's `Cmd+F` (works in VS Code's integrated terminal across native scrollback only)
- You want to **mouse-select and copy a large region** of the conversation across many messages
- You want to **share a snippet** by copying terminal output
- You want to **scroll with the mouse wheel** across the whole conversation history

For those cases, the conversation needs to live in the terminal's *native* scrollback buffer, not in Claude's internal viewport. That's what the next section is for.

## The `Ctrl+O → [` trick (for terminal-level scrollback)

In fullscreen mode (`NO_FLICKER=1`), Claude Code provides an in-app way to dump the full conversation into the **normal buffer**, where standard terminal scrollback then works.

### The cycle

`Ctrl+O` cycles through three states (per [official docs](https://code.claude.com/docs/en/interactive-mode)):

| Press | State | What you see |
|---|---|---|
| 0 (default) | **Focus view** | Just the LAST user message + tool calls + assistant response. Minimal info. No input box. |
| 1 | **Normal prompt** | Input box at bottom, recent messages above, status line. Shows "Sautéed for Xs" / "Crunched for Xs" timing messages. The live working view. |
| 2 | **Transcript mode** | Full scrollable conversation history with `/` search. Most detail — all tool calls expanded. Bottom bar says "Showing detailed transcript". Press `[` here to dump to native scrollback. |
| 3 (= back to 0) | Focus view | |

### Dumping history to native scrollback

1. Inside Claude Code, press **`Ctrl+O`** twice → enters transcript mode (past focus view and normal prompt)
2. Press **`[`** → Claude walks the entire JSONL message-by-message and prints each one as ANSI-styled text into the normal buffer
3. Press **`Esc`** to leave transcript mode and return to the live prompt
4. The normal buffer now contains the entire conversation. Scroll up in your terminal — you can reach the very first message of the session.

### How it works under the hood

When you press `[`, Claude switches OUT of the alt buffer briefly, walks the JSONL, and prints each message as **flat ANSI-colored text** to the normal buffer. The same React/Ink rendering pipeline that normally draws to the alt buffer with cursor positioning now produces linear printable output instead. Then Claude switches back to the alt buffer for the live TUI.

From the normal buffer's perspective: a CLI tool just suddenly emitted the entire transcript as text. xterm.js captures it in scrollback like any other terminal output. Pretty rendering survives because terminals interpret ANSI color codes the same way regardless of how the cursor got there.

### Caveats

- **It must be triggered manually.** There's no setting to make `claude -r` automatically paint history on startup. Anthropic chose this because dumping the history of a long session adds visible latency every time you resume.
- **It's only available in fullscreen mode** (`NO_FLICKER=1`). With `NO_FLICKER=0`, the dual-buffer mechanism the trick relies on doesn't exist.
- **One open bug report** ([anthropics/claude-code#42670](https://github.com/anthropics/claude-code/issues/42670)) claims `[` doesn't work on some setups. Test before relying on it. As of our investigation it worked correctly in VS Code's integrated terminal.
- **Each dump appends — it does NOT clear first.** Every `[` press appends the entire transcript to the normal buffer. If you press `[` six times, you get six copies of the full transcript in scrollback. This fills up the scrollback buffer quickly and makes `Cmd+F` search return dozens of false duplicates. **Always run `clear` before dumping** to wipe the previous dump: type `clear` in the terminal (or `Cmd+K` in VS Code), then re-enter Claude, then `Ctrl+O → [`. This ensures a single clean copy.

### Hard ~250-line cap

Independent of the dump trick, Claude Code's TUI has an internal cap of approximately 250 lines visible in the live render at once. Scrolling within Claude's UI (not native scrollback) is bounded by this cap. The dump trick bypasses it because it writes to native scrollback, which is bound only by your terminal's setting.

See:
- [anthropics/claude-code#28077](https://github.com/anthropics/claude-code/issues/28077) — open, canonical tracking issue
- [anthropics/claude-code#40253](https://github.com/anthropics/claude-code/issues/40253) — closed as duplicate
- [anthropics/claude-code#8937](https://github.com/anthropics/claude-code/issues/8937) — closed "Not Planned"

## Recommended VS Code terminal setup

These settings make Claude Code's `Ctrl+O → [` workflow as smooth as possible in the VS Code integrated terminal.

### `~/.claude/settings.json`

```json
{
  "env": {
    "CLAUDE_CODE_NO_FLICKER": "1"
  }
}
```

Explicitly setting `=1` future-proofs against Anthropic ever flipping the default. Even though it's the default since v2.1.89, having it explicit means your config is intentional and survives version changes.

### `%APPDATA%\Code\User\settings.json` (Windows) or `~/.config/Code/User/settings.json` (Linux/Mac)

```json
{
  "terminal.integrated.scrollback": 250000,
  "terminal.integrated.persistentSessionScrollback": 250000,
  "terminal.integrated.gpuAcceleration": "canvas"
}
```

| Setting | Why |
|---|---|
| `scrollback: 250000` | Default 1000 is way too small for any long Claude session. 250k catches even outlier marathon sessions while only using ~40 MB per terminal. Scrollback grows lazily — empty terminals don't pay the cost upfront. |
| `persistentSessionScrollback: 250000` | Default 100. Without raising this, your scrollback shrinks to 100 lines on every VS Code window reload. Match it to `scrollback`. |
| `gpuAcceleration: "canvas"` | Default `"auto"` uses WebGL, which has lazy/batched repaints. This causes Ctrl+End to require a follow-up keystroke before the scroll position visually updates. Canvas does immediate-mode painting which is more responsive for terminal interactions. Costs almost nothing on modern hardware. |

### `%APPDATA%\Code\User\keybindings.json`

```json
[
  {
    "key": "ctrl+o",
    "command": "-workbench.action.files.openFile"
  }
]
```

VS Code's default `Ctrl+O` is "Open File", which intercepts the keystroke before it reaches the terminal. Without unbinding it, pressing `Ctrl+O` while focused on the terminal opens VS Code's file picker instead of activating Claude's transcript mode. The unbind releases it globally so the terminal can capture it.

If you want to be more conservative, scope the unbind to only the terminal:

```json
{
  "key": "ctrl+o",
  "command": "-workbench.action.files.openFile",
  "when": "terminalFocus"
}
```

But the global unbind is simpler and you almost never use Ctrl+O for File → Open in practice.

## Interactive mode keyboard shortcuts

A reference of the most useful keystrokes inside an active Claude Code session, beyond the obvious arrow keys and Enter.

### Editing the current input line

| Key | Action |
|---|---|
| **`Ctrl+L`** | **Clear the entire prompt input** (cursor-position-independent). Wipes whatever you've typed. Conversation history is untouched. |
| `Ctrl+U` | Delete from cursor to start of line. Stores the deleted text for paste with `Ctrl+Y`. Repeat for multiline. |
| `Ctrl+K` | Delete from cursor to end of line. Stores for paste. |
| `Ctrl+Y` | Paste back what `Ctrl+U` / `Ctrl+K` killed. |
| `Alt+B` | Move cursor back one word. (Mac: needs Option-as-Meta in your terminal.) |
| `Alt+F` | Move cursor forward one word. |

### History search

| Key | Action |
|---|---|
| **`Ctrl+R`** | **Reverse history search** through previous prompts. Bash-style. |
| `Ctrl+R` (again) | **Cycle to the next older match** with the current filter. Keep pressing to walk through all matches. |
| `Tab` or `Esc` | Accept the current match and return to editing it. |
| `Enter` | Accept and submit immediately. |
| `Ctrl+C` | Cancel reverse search, restore original input. |

### Viewport navigation (the simple "scroll back" answer)

| Key | Action |
|---|---|
| **`Ctrl+Home`** | **Jump to the very first message** of the conversation, in the default view. No transcript mode needed. |
| **`Ctrl+End`** | Jump back to the live prompt position. |
| `Page Up` / `Page Down` | Scroll one page up/down through the conversation. |
| Up/Down arrows (when viewport-scrolled) | Scroll line by line. |

This is the default-view scrolling. Works regardless of `CLAUDE_CODE_NO_FLICKER` setting and bypasses the alt-buffer scrollback issue entirely because Claude is walking its own in-memory message tree, not relying on terminal scrollback.

### Transcript / focus modes (for terminal-level scrollback dump)

| Key | Action |
|---|---|
| **`Ctrl+O`** (1st press) | **Enter transcript mode** — Claude's own scrollable history view, with `/` to search. |
| `[` (in transcript mode) | **Dump full conversation to native terminal scrollback** — for when you specifically need the content in your terminal buffer (mouse-scroll, terminal `Cmd+F`, copy across many messages). |
| `Esc` (in transcript mode) | Exit back to normal prompt. |
| `Ctrl+O` (2nd press from normal) | **Focus view** — last user message + tool calls + assistant response only, with tools shown in full detail. |
| `Ctrl+O` (3rd press) | Back to normal prompt. |

For most "look back" tasks, prefer the simple `Ctrl+Home` viewport navigation above. The Ctrl+O cycle is for the niche power-user case of wanting the conversation in terminal scrollback.

> ⚠️ The Claude Code in-app help (`?`) labels Ctrl+O as "Toggle verbose output" — this is misleading. There's a separate "verbose mode" toggled via `/config` → "Verbose output" or the `--verbose` CLI flag. Ctrl+O is the transcript/focus cycle, not real-time verbose streaming. Open bug: [anthropics/claude-code#14511](https://github.com/anthropics/claude-code/issues/14511).

### Verbose output (the actual one)

| Mechanism | What it does |
|---|---|
| `--verbose` CLI flag | Start a session with full tool input/output streamed live (instead of the default truncated view). |
| `/config` → "Verbose output" | Toggle the same setting interactively. |

This is **not** the same as `Ctrl+O` focus view. Focus view shows the last exchange in detail; verbose mode streams every tool call's full input/output for the entire session.

## Known limitations and accepted quirks

### PowerShell + PSReadLine bleed-through

When running `claude -r` from PowerShell in VS Code's integrated terminal, you may see the PowerShell prompt visually bleed through one of the picker entries. Pattern:

```
PS C:\Users\user> █ like having to do this every time
4 seconds ago · HEAD · 3MB
```

Where `PS C:\Users\user> █` overlays the start of `i dont like having to do this every time`.

**Cause (likely):** PSReadLine, PowerShell's enhanced input library, is asynchronous and continues to render its prompt + input cursor at the row where it was when `claude` was launched. Claude's TUI draws picker entries using row-by-row redraws that don't fully blank the leading-margin cells, so PSReadLine's prompt remains visible underneath.

**Verification status:** UNVERIFIED. We confirmed it's not a VS Code GPU rendering issue (canvas/WebGL/DOM all reproduce it) and not a VS Code shell integration issue (disabling it didn't help). The most likely culprit is PSReadLine's input handling but we didn't fully prove it.

**Workarounds:**
- Run `clear` (or `cls` in PowerShell) before `claude -r` — gives Claude a blank canvas with nothing to bleed through *initially*
- Wrap it: `function cr { Clear-Host; claude -r @args }` in `$PROFILE`
- Switch to a different shell for Claude: Git Bash or cmd.exe in VS Code, or Windows Terminal entirely. Other shells without PSReadLine don't reproduce the bug.

This is accepted as a quirk; we stopped trying to fix it.

### Ctrl+End repaint lag

In VS Code's integrated terminal with default WebGL rendering, pressing `Ctrl+End` to scroll to the bottom updates xterm.js's internal scroll position immediately but doesn't trigger a visible repaint until the next event (any keystroke or mouse move). The `gpuAcceleration: "canvas"` recommendation above mitigates this with eager immediate-mode rendering.

## Source citations

| Topic | Source |
|---|---|
| Fullscreen rendering / `CLAUDE_CODE_NO_FLICKER` | https://code.claude.com/docs/en/fullscreen |
| Interactive mode keystrokes | https://code.claude.com/docs/en/interactive-mode |
| Scrollback bypass / alt buffer regression | [anthropics/claude-code#28077](https://github.com/anthropics/claude-code/issues/28077) (open, canonical) |
| 250-line cap | [anthropics/claude-code#40253](https://github.com/anthropics/claude-code/issues/40253) |
| Resume doesn't paint history | [anthropics/claude-code#8937](https://github.com/anthropics/claude-code/issues/8937) (closed Not Planned) |
| `Ctrl+O` mislabeled in `?` help | [anthropics/claude-code#14511](https://github.com/anthropics/claude-code/issues/14511) |
| `Ctrl+R` reverse search | [Interactive mode docs § Reverse search](https://code.claude.com/docs/en/interactive-mode#reverse-search-with-ctrlr) |

## See also

- [`docs/sessions.md`](sessions.md) — Claude Code session storage internals, JSONL format, and the `/transplant` workflow
- [`docs/hacks.md`](hacks.md) — quick-reference hacks
- `session_transplant.py` — JSONL transplant script
