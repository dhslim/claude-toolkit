# Clawd "Jump to Terminal" window-focus patch

Makes **Clawd on Desk**'s *Jump to Terminal* button focus the **correct VS Code
window** — instantly, with no wrong-window flicker, across up to 44 windows.

## The bug

Every VS Code project window shares **one `Code.exe` process**. Clawd's desktop
app focuses a session by raising that process's `MainWindowHandle`, which Windows
returns as whatever window is currently in front — so "Jump to Terminal" lands on
the wrong window (or a dead/closed handle). A breadth audit found **4 of 6** live
sessions focusing the wrong window. Write-ups:
`../../docs/clawd_focus_postmortem.html` (root-cause forensics) and
`../../docs/clawd_focus_window_fix.html` (the full fix, byte recipes included).

## The fix (two halves)

### 1. Extension patch (v4) — the thing that actually focuses

Clawd ships a tiny VS Code extension — **Clawd Terminal Focus**
(`clawd.clawd-terminal-focus`, side-loaded via VSIX by the desktop app) — that it
calls on every Jump-to-Terminal to reveal the right terminal *tab* via
`terminal.show()`. The matching extension instance is, by definition, **already
running in the correct window**. The patch makes it also **raise that window**:
it matches the workspace folder basename against the visible window titles and
`SetForegroundWindow`s it (with the `AttachThreadInput` trick to beat Windows'
foreground lock). A **persistent warm PowerShell helper** compiles the Win32 code
once at startup, so each raise is ~instant. `PORT_RANGE` is bumped 5 → 44 so more
than five windows can each bind a focus port.

### 2. app.asar byte edits — routing + flicker removal

Three **same-length, in-place byte overwrites** to the packed desktop app
(no asar repack; offsets stay valid because the total length never changes):

| Edit | Change | Effect |
|------|--------|--------|
| A | broadcast loop `port <= 23460` → `23499` | app reaches all 44 extension ports, not 5 |
| B | focus-tab delay `}, 800)` → `}, 200)` | correct focus arrives ~600 ms sooner |
| C | `WinFocus::Focus` guard `hWnd ==` → `hWnd !=` | **neuters Clawd's own wrong-window raise** → zero flicker |

After Edit C the extension is the *only* thing that focuses windows: Clawd's
broken stage-1 raise becomes a silent no-op (A → C, no wrong-window B). Side
effect: Clawd's built-in focus is off for all editors — fine for a VS-Code-only
setup, since the extension covers VS Code.

## Files

| File | Purpose |
|------|---------|
| `extension.orig.js`    | Known-good stock extension (v0.1.1) — used to detect Clawd updates |
| `extension.patched.js` | The patched extension (v4) that gets deployed |
| `apply.ps1`            | Idempotent (re)apply / `-Revert` for **both halves** (extension + the 3 asar edits) |

## Patched targets

1. **Installed extension** (what VS Code loads): `~\.vscode\extensions\clawd.clawd-terminal-focus-0.1.1\extension.js`
2. **Bundled extension source** (re-deployed on Clawd startup): `...\Clawd on Desk\resources\app.asar.unpacked\extensions\vscode\extension.js`
3. **Packed app**: `...\Clawd on Desk\resources\app.asar` (edits A/B/C above)

Both extension copies must carry the patch, or a Clawd restart silently reverts
the installed one. Everything is backed up first (`<file>.clawd-orig` +
timestamped copies for the extension; `app.asar.clawd-orig` for the app).

## Usage

```powershell
# Apply (or re-apply after a Clawd on Desk update):
powershell -ExecutionPolicy Bypass -File .\apply.ps1

# Roll back everything to stock:
powershell -ExecutionPolicy Bypass -File .\apply.ps1 -Revert
```

Then activate:
1. **Restart the Clawd app** (asar edits load on app start).
2. **Reload each VS Code window** (`Ctrl+Shift+P` → *Developer: Reload Window*)
   so the extension host loads the patched extension and binds a port.

`apply.ps1` is safe to re-run any time: it skips what's already applied, anchors
every asar edit to unique context and **warns instead of writing** if Clawd's
code changed shape (→ re-derive that edit), and refuses to overwrite an
extension file that matches neither the stock original nor the patch. On
`-Revert` it will not restore a stale asar backup over a freshly-updated asar.

## Known limitations

- **SSH-remote windows** (`[SSH: host]`) aren't handled: the extension would run
  on the remote host (no Win32), and the PIDs are remote. Would need a separate
  title-based path.
- **Worktrees with no open window** have no window to raise — unavoidable.
- **Clawd updates revert everything** (new asar + re-deployed extension) —
  re-run `apply.ps1`, restart Clawd, reload windows. Breakage is obvious, not
  silent: window-focus stops working again.
- The extension keeps one tiny persistent `powershell.exe` per window (the warm
  raise helper) — by design, it's what makes the raise instant.

## Verify

1. `apply.ps1`, restart Clawd, reload all VS Code windows.
2. From a *different* window, click **Jump to Terminal** on a background
   project's Clawd card → the correct window comes forward, terminal tab shown,
   **no flash of any other window**.
3. Debug log (helper spawns + raises): `%TEMP%\clawd-focus-debug.log`.
