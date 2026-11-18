@echo off
REM open_in_code.cmd -- EDITOR/VISUAL wrapper for Claude Code.
REM
REM Claude Code launches the editor as:  <EDITOR> "<file>"   (shell:true)
REM so there is nowhere to inject a line number. VS Code needs `--goto FILE:LINE`
REM to land anywhere but line 1 -- useless for a transcript whose newest content
REM is at the END.
REM
REM VS Code's --goto parser accepts ONLY non-negative integers; `-1`, `$` and
REM `end` are all rejected. It does, however, CLAMP an out-of-range line to the
REM last one -- so a deliberately huge constant is the "end of file" sentinel.
REM
REM Counting the real line count was tried and rejected: `find /c /v ""` and
REM PowerShell disagree by ~8k lines on a terminal transcript (lone CR from
REM progress rendering is a line break to one and not the other), and VS Code
REM has its own rule. Any count we computed would be a guess that could land
REM mid-file; the clamp is exact and costs nothing.
REM
REM   -r / --reuse-window : open as a tab in the existing window
REM   -g / --goto         : jump to FILE:LINE
setlocal

set "F=%~1"
if "%F%"=="" (
  REM No file given -- just surface the window.
  code -r
  exit /b 0
)

if not exist "%F%" (
  code -r "%F%"
  exit /b 0
)

code -r -g "%F%:999999999"
exit /b 0
