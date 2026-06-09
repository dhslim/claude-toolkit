#Requires -Version 5
[CmdletBinding()]
param([switch]$Revert)

# (Re)apply the Clawd "raise the correct VS Code window" patch to the Clawd
# Terminal Focus extension. Safe to run any time:
#   - idempotent (skips files already patched)
#   - refuses to clobber a file that no longer matches the known original
#     (i.e. Clawd shipped a new extension.js -> you re-derive the patch)
#   - backs up before writing; -Revert restores the stock extension
#
# Run after every Clawd on Desk update (the app re-deploys its bundled VSIX,
# which reverts the installed copy). Then reload your VS Code windows.

$ErrorActionPreference = "Stop"
$here    = $PSScriptRoot
$patched = Join-Path $here "extension.patched.js"
$orig    = Join-Path $here "extension.orig.js"
$MARKER  = "clawd-focus-window-raise patch"

# Both copies that must carry the patch:
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

Write-Host ""
Write-Host "Done. Reload each VS Code window:  Ctrl+Shift+P -> 'Developer: Reload Window'"
Write-Host "(or fully restart VS Code) so the extension host reloads the patched extension.js."
