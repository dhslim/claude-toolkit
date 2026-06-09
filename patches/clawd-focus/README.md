# Clawd "Jump to Terminal" window-focus patch

Makes **Clawd on Desk**'s *Jump to Terminal* button focus the **correct VS Code
window**, not just the correct terminal tab.

## The bug

Every VS Code project window shares **one `Code.exe` process**. Clawd's desktop
app focuses a session by raising that process's `MainWindowHandle`, which Windows
returns as whatever window is currently in front — so "Jump to Terminal" lands on
the wrong window (or a dead/closed handle). A breadth audit found **4 of 6** live
sessions focusing the wrong window. Full write-up: `../../docs/clawd_focus_postmortem.html`.

## The fix

Clawd already ships a tiny VS Code extension — **Clawd Terminal Focus**
(`clawd.clawd-terminal-focus`, installed via VSIX by the desktop app) — that it
calls on every Jump-to-Terminal to reveal the right terminal *tab* via
`terminal.show()`. The matching extension instance is, by definition, **already
running in the correct window**.

So this patch adds one step to that extension's `focusTerminalByPids`: after
`terminal.show()`, also **raise this window to the foreground**. It finds the
window by matching the workspace folder basename against the visible `Code.exe`
window titles, then `SetForegroundWindow`s it (using the `AttachThreadInput`
trick to defeat Windows' foreground lock). Best-effort, fire-and-forget; it can
never break the existing tab-focus.

## Files

| File | Purpose |
|------|---------|
| `extension.orig.js`    | Known-good stock extension (v0.1.1) — used to detect Clawd updates |
| `extension.patched.js` | The patched extension that gets deployed |
| `apply.ps1`            | Idempotent (re)apply / `-Revert` script |

## Patched targets (two copies, on purpose)

1. **Installed** (what VS Code loads): `~\.vscode\extensions\clawd.clawd-terminal-focus-0.1.1\extension.js`
2. **Bundled source** (re-deployed on Clawd startup): `...\Clawd on Desk\resources\app.asar.unpacked\extensions\vscode\extension.js`

Both must carry the patch, or a Clawd restart silently reverts the installed copy.
Each is backed up to `<file>.clawd-orig` (true original) plus a timestamped copy.

## Usage

```powershell
# Apply (or re-apply after a Clawd update):
powershell -ExecutionPolicy Bypass -File .\apply.ps1

# Roll back to stock:
powershell -ExecutionPolicy Bypass -File .\apply.ps1 -Revert
```

After running, **reload each VS Code window** (`Ctrl+Shift+P` -> *Developer:
Reload Window*) so the extension host loads the new code.

`apply.ps1` is safe: it skips files already patched, and **refuses to overwrite**
a file that no longer matches `extension.orig.js` (meaning Clawd shipped a new
version — re-derive the patch and refresh the two `.js` files here).

## Known limitations

- **~800 ms flash.** Clawd's broken desktop-side focus still fires first and
  raises the wrong window; this patch corrects it ~800 ms later (the delay is
  baked into the desktop app). Removing the flash entirely would require patching
  the packed `app.asar` — intentionally out of scope.
- **Worktrees with no open window** (e.g. a git worktree you never opened as a
  folder) have no window to raise — unavoidable by any method until one exists.
- **Overwritten by Clawd updates** — re-run `apply.ps1`, then reload windows.

## Verify

1. `apply.ps1`, then reload all VS Code windows.
2. From a *different* window, click **Jump to Terminal** on a background
   project's Clawd card -> the correct window should come forward and its
   terminal tab should be shown.
3. Test the original failing case (claude-toolkit) plus one or two others.
