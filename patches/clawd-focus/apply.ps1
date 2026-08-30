#Requires -Version 5
[CmdletBinding()]
param([switch]$Revert)

# (Re)apply the full Clawd "Jump to Terminal -> correct VS Code window" fix.
# Two halves, both covered here:
#
#   1. EXTENSION (Clawd Terminal Focus, both copies): the v4 patched
#      extension.js that raises its own window via a persistent warm
#      PowerShell helper, PORT_RANGE 44.
#   2. APP.ASAR (3 same-length in-place byte edits to the packed desktop app):
#        A. broadcast port ceiling  "port <= 23460" -> "port <= 23499"  (5 -> 44 windows)
#        B. focus-tab delay         "}, 800)"       -> "}, 200)"        (snappier)
#        C. WinFocus::Focus guard   "hWnd == IntPtr.Zero) return"
#                                -> "hWnd != IntPtr.Zero) return"
#           (neuters Clawd's own wrong-window raise -> no flicker; the
#            extension is then the ONLY thing that focuses windows)
#        D. Sessions window width   "DEFAULT_WIDTH = 480" -> "= 960"
#           (cosmetic: the dashboard/Sessions window opens wide by default)
#
# Safe to run any time:
#   - idempotent (skips anything already patched/applied)
#   - every asar edit is anchored to unique context and aborts (warns) if the
#     anchor is missing or ambiguous (i.e. Clawd shipped new code -> re-derive)
#   - same-length byte overwrites only, so the asar header offsets stay valid
#   - backs up before writing; -Revert restores stock (extension + asar)
#
# Run after every Clawd on Desk update, then:
#   - restart the Clawd app   (activates the asar edits)
#   - reload VS Code windows  (activates the extension; Ctrl+Shift+P -> Reload Window)
# Full write-up: ../../docs/clawd_focus_window_fix.html

$ErrorActionPreference = "Stop"
$here    = $PSScriptRoot
$patched = Join-Path $here "extension.patched.js"
$orig    = Join-Path $here "extension.orig.js"
$MARKER  = "clawd-focus-window-raise patch"

$asarPath = "C:\Users\user\AppData\Local\Programs\Clawd on Desk\resources\app.asar"

# Both extension copies that must carry the patch:
#   1. the installed copy VS Code actually loads
#   2. the app's bundled VSIX source (re-deployed on Clawd startup)
$targets = @(
  "C:\Users\user\.vscode\extensions\clawd.clawd-terminal-focus-0.1.1\extension.js",
  "C:\Users\user\AppData\Local\Programs\Clawd on Desk\resources\app.asar.unpacked\extensions\vscode\extension.js"
)

function Read-Norm([string]$path) {
  if (-not (Test-Path $path)) { return $null }
  return ((Get-Content $path -Raw) -replace "`r`n", "`n").TrimEnd()
}
function Write-NoBom([string]$path, [string]$text) {
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}
function CountOcc([string]$s, [string]$pat) {
  $n = 0; $i = 0
  while (($i = $s.IndexOf($pat, $i)) -ge 0) { $n++; $i += $pat.Length }
  return $n
}

# ---------------------------------------------------------------------------
# Part 1: extension.js (both copies)
# ---------------------------------------------------------------------------
Write-Host "=== extension (Clawd Terminal Focus) ==="
$patchedRaw = Get-Content $patched -Raw
$origRaw    = Get-Content $orig -Raw
$origNorm   = (Read-Norm $orig)
$stamp      = Get-Date -Format "yyyyMMdd-HHmmss"

foreach ($t in $targets) {
  if (-not (Test-Path $t)) { Write-Warning "MISSING (skipped): $t"; continue }
  $curNorm = Read-Norm $t

  if ($Revert) {
    $bak = "$t.clawd-orig"
    if (Test-Path $bak) { Copy-Item $bak $t -Force; Write-Host "reverted from backup: $t" }
    else { Write-NoBom $t $origRaw; Write-Host "reverted to known original: $t" }
    continue
  }

  if ($curNorm -like "*$MARKER*") { Write-Host "already patched (skipped): $t"; continue }
  if ($curNorm -ne $origNorm) {
    Write-Warning ("DIFFERS from known original - Clawd likely updated this file. " +
      "Skipping to avoid clobbering. Re-derive the patch from the new extension.js, then " +
      "refresh extension.orig.js / extension.patched.js here.  ($t)")
    continue
  }

  if (-not (Test-Path "$t.clawd-orig")) { Copy-Item $t "$t.clawd-orig" -Force }
  Copy-Item $t "$t.clawd-bak-$stamp" -Force
  Write-NoBom $t $patchedRaw
  Write-Host "patched: $t   (backup: $t.clawd-bak-$stamp)"
}

# ---------------------------------------------------------------------------
# Part 2: app.asar (3 same-length byte edits)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== app.asar (desktop app byte edits) ==="
if (-not (Test-Path $asarPath)) {
  Write-Warning "app.asar not found - is Clawd on Desk installed?  ($asarPath)"
} else {
  $latin   = [System.Text.Encoding]::GetEncoding(28591)  # 1 char == 1 byte
  $bytes   = [System.IO.File]::ReadAllBytes($asarPath)
  $s       = $latin.GetString($bytes)
  $asarBak = "$asarPath.clawd-orig"

  if ($Revert) {
    # Only restore the backup onto an asar that actually carries our edits;
    # a fresh Clawd update ships a new (stock) asar that a stale backup
    # would silently downgrade.
    $hasEdits = $s.Contains("port <= 23499") -or $s.Contains("hWnd != IntPtr.Zero) return") -or $s.Contains("DEFAULT_WIDTH = 960")
    if (-not (Test-Path $asarBak)) { Write-Warning "no asar backup ($asarBak) - nothing to revert" }
    elseif (-not $hasEdits) { Write-Host "asar looks stock/new (no edits present) - stale backup NOT restored" }
    else { Copy-Item $asarBak $asarPath -Force; Write-Host "asar reverted from backup. Restart the Clawd app." }
  } else {
    $changed = $false

    # --- Edit A: broadcast port ceiling 23460 -> 23499 ---------------------
    if ($s.Contains("port <= 23499")) {
      Write-Host "already applied: broadcast range (port <= 23499)"
    } elseif ((CountOcc $s "port <= 23460") -eq 1) {
      $i = $s.IndexOf("port <= 23460")          # "port <= 23460": '6' at +11, '0' at +12
      $bytes[$i + 11] = 57; $bytes[$i + 12] = 57   # both -> '9'
      $changed = $true
      Write-Host "applied: broadcast range 23460 -> 23499 (44 windows)"
    } else {
      Write-Warning "broadcast-range anchor 'port <= 23460' not found/unique - Clawd changed; re-derive this edit"
    }

    # --- Edit B: focus-tab delay 800 -> 200 --------------------------------
    $ft = $s.IndexOf("/focus-tab")
    if ($ft -lt 0) {
      Write-Warning "'/focus-tab' anchor not found - Clawd changed; re-derive this edit"
    } else {
      $win = $s.Substring($ft, [Math]::Min(600, $s.Length - $ft))
      if ($win.Contains("}, 200)")) {
        Write-Host "already applied: focus delay (}, 200))"
      } elseif ($win.Contains("}, 800)")) {
        $abs = $ft + $win.IndexOf("}, 800)") + 3   # position of the '8'
        $bytes[$abs] = 50                          # '8' -> '2'
        $changed = $true
        Write-Host "applied: focus delay 800ms -> 200ms"
      } else {
        Write-Warning "focus-delay anchor '}, 800)' not found near /focus-tab - re-derive this edit"
      }
    }

    # --- Edit C: neuter WinFocus::Focus (guard == -> !=) --------------------
    if ((CountOcc $s "SetForegroundWindow(hWnd)") -ne 1) {
      Write-Warning "'SetForegroundWindow(hWnd)' not unique - Clawd changed; re-derive this edit"
    } else {
      $sfw = $s.IndexOf("SetForegroundWindow(hWnd)")
      $gNe = $s.LastIndexOf("hWnd != IntPtr.Zero) return", $sfw)
      $gEq = $s.LastIndexOf("hWnd == IntPtr.Zero) return", $sfw)
      if ($gNe -ge 0 -and $gNe -gt $gEq -and ($sfw - $gNe) -lt 320) {
        Write-Host "already applied: stage-1 focus neutered (guard !=)"
      } elseif ($gEq -ge 0 -and ($sfw - $gEq) -gt 40 -and ($sfw - $gEq) -lt 320 -and
                $s.Substring($gEq, $sfw - $gEq).Contains("IsIconic") -and
                $s.Substring($gEq, $sfw - $gEq).Contains("keybd_event")) {
        $bytes[$gEq + 5] = 33                      # '=' -> '!'  ("hWnd ==" -> "hWnd !=")
        $changed = $true
        Write-Host "applied: stage-1 WinFocus::Focus neutered (no wrong-window raise / flicker)"
      } else {
        Write-Warning "Focus-guard anchor not found in expected shape - Clawd changed; re-derive this edit"
      }
    }

    # --- Edit D: Sessions (dashboard) window default width 480 -> 960 -------
    if ($s.Contains("DEFAULT_WIDTH = 960")) {
      Write-Host "already applied: Sessions window width (960)"
    } elseif ((CountOcc $s "DEFAULT_WIDTH = 480") -eq 1) {
      $i = $s.IndexOf("DEFAULT_WIDTH = 480") + 16   # the '4' of 480
      $bytes[$i] = 57; $bytes[$i + 1] = 54           # '48' -> '96'  => 960
      $changed = $true
      Write-Host "applied: Sessions window default width 480 -> 960"
    } else {
      Write-Warning "Sessions-width anchor 'DEFAULT_WIDTH = 480' not found/unique - Clawd changed; re-derive this edit"
    }

    if ($changed) {
      if (-not (Test-Path $asarBak)) { Copy-Item $asarPath $asarBak -Force; Write-Host "asar backed up: $asarBak" }
      [System.IO.File]::WriteAllBytes($asarPath, $bytes)
      Write-Host "asar written (length unchanged: $($bytes.Length) bytes)"
    }
  }
}

Write-Host ""
if ($Revert) {
  Write-Host "Done (revert). Restart the Clawd app + reload VS Code windows."
} else {
  Write-Host "Done. To activate anything newly applied:"
  Write-Host "  1. restart the Clawd app          (asar edits: ports / delay / no-flicker)"
  Write-Host "  2. reload each VS Code window      (extension: Ctrl+Shift+P -> 'Developer: Reload Window')"
}
