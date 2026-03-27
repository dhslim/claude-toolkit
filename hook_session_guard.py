#!/usr/bin/env python3
"""SessionStart hook — warns if another Claude Code instance is already using
the same session ID (e.g. opened in two terminals).

Writes a PID lock file per session. If a lock already exists with a live
process, emits a blocking warning so the user can dismiss or switch to
cfork/cread instead.

Fork-aware: cfork/cread use --fork-session which creates a new session from
an existing one. Claude Code fires SessionStart with the *original* session
ID before forking. We detect --fork-session in the cmdline and skip the
warning in that case, since the fork will get its own session ID.

Platform notes:
- Linux: uses /proc to walk the process tree and find the real claude PID.
  os.getppid() returns the intermediate shell PID (which dies immediately),
  so we must walk up to find the actual claude process.
- macOS: os.getppid() returns claude directly, but we use ps-based tree
  walking as a unified fallback for consistency.
- Windows: uses ctypes (CreateToolhelp32Snapshot) to walk the process tree.
  No subprocess calls needed — pure Win32 API, fast and silent.
"""

import json
import os
import platform
import sys
from pathlib import Path

IS_WINDOWS = platform.system() == 'Windows'

LOCK_DIR = Path.home() / '.claude' / 'session-locks'


if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

    # Win32 constants
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    MAX_PATH = 260

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ('dwSize', ctypes.wintypes.DWORD),
            ('cntUsage', ctypes.wintypes.DWORD),
            ('th32ProcessID', ctypes.wintypes.DWORD),
            ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
            ('th32ModuleID', ctypes.wintypes.DWORD),
            ('cntThreads', ctypes.wintypes.DWORD),
            ('th32ParentProcessID', ctypes.wintypes.DWORD),
            ('pcPriClassBase', ctypes.c_long),
            ('dwFlags', ctypes.wintypes.DWORD),
            ('szExeFile', ctypes.c_char * MAX_PATH),
        ]

    kernel32 = ctypes.windll.kernel32

    def _win_get_process_info(pid):
        """Get (parent_pid, exe_name) for a given PID using snapshot. Returns (None, None) on failure."""
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return None, None
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return None, None
            while True:
                if entry.th32ProcessID == pid:
                    name = entry.szExeFile.decode('utf-8', errors='replace').lower()
                    return entry.th32ParentProcessID, name
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return None, None

    def _win_get_cmdline(pid):
        """Get command line for a PID. Returns empty string on failure."""
        try:
            import subprocess
            # Single quick call, hidden window
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine'],
                capture_output=True, text=True, timeout=3,
                creationflags=0x08000000,
            )
            return result.stdout.strip()
        except Exception:
            return ''


def _pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    if IS_WINDOWS:
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _find_claude_ancestor():
    """Walk up the process tree to find the claude process.

    Returns (pid, cmdline_args) or (None, []) if not found.
    cmdline_args includes all arguments so we can check for --fork-session.
    """
    try:
        pid = os.getpid()
        if IS_WINDOWS:
            # Windows: use CreateToolhelp32Snapshot (pure ctypes, no subprocess)
            for _ in range(15):
                ppid, name = _win_get_process_info(pid)
                if ppid is None or ppid <= 1:
                    break
                if name == 'claude.exe':
                    # Only fetch cmdline when we find claude (one subprocess call max)
                    cmdline = _win_get_cmdline(ppid)
                    if '--type=' not in cmdline:
                        args = cmdline.split() if cmdline else []
                        return ppid, args
                pid = ppid
        elif Path('/proc/self/stat').exists():
            # Linux: use /proc for fast lookups
            for _ in range(10):
                pid = int(Path(f'/proc/{pid}/stat').read_text().split()[3])
                if pid <= 1:
                    break
                try:
                    cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
                    args = [a.decode() for a in cmdline.split(b'\x00') if a]
                    if args and (args[0].endswith('/claude') or args[0] == 'claude'):
                        return pid, args
                except (OSError, UnicodeDecodeError):
                    continue
        else:
            # macOS: use ps to walk the tree (gets comm only, then full args)
            import subprocess
            for _ in range(10):
                result = subprocess.run(
                    ['ps', '-o', 'ppid=,comm=', '-p', str(pid)],
                    capture_output=True, text=True, timeout=2,
                )
                if not result.stdout.strip():
                    break
                parts = result.stdout.strip().split(None, 1)
                if len(parts) < 2:
                    break
                ppid, comm = int(parts[0]), parts[1]
                if ppid <= 1:
                    break
                if comm.endswith('/claude') or comm == 'claude':
                    # Get full args
                    args_result = subprocess.run(
                        ['ps', '-o', 'args=', '-p', str(ppid)],
                        capture_output=True, text=True, timeout=2,
                    )
                    args = args_result.stdout.strip().split() if args_result.stdout.strip() else []
                    return ppid, args
                pid = ppid
    except Exception:
        pass
    return None, []


def _get_process_tty(pid: int) -> str:
    """Try to get the TTY of a process for a more helpful warning."""
    if IS_WINDOWS:
        return ''  # No TTY concept on Windows
    try:
        import subprocess
        result = subprocess.run(
            ['ps', '-o', 'tty=', '-p', str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        tty = result.stdout.strip()
        if tty and tty != '??':
            return tty
    except Exception:
        pass
    return ''


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    session_id = hook_data.get('session_id')
    if not session_id:
        return

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_DIR / f'{session_id}.pid'

    # Find the actual claude process PID and its command line args
    claude_pid, claude_args = _find_claude_ancestor()
    claude_pid = claude_pid or os.getppid()

    # cfork/cread use --fork-session — they'll get a new session ID,
    # so don't warn about the original session being "in use"
    if '--fork-session' in claude_args:
        return

    # Check for existing lock
    if lock_file.exists():
        try:
            existing_pid = int(lock_file.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid and existing_pid != claude_pid and _pid_alive(existing_pid):
            tty = _get_process_tty(existing_pid)
            tty_hint = f' (terminal: {tty})' if tty else ''
            sid_short = session_id[:8]

            reason = (
                f"Another Claude Code instance is already using session {sid_short}{tty_hint} (PID {existing_pid}). "
                f"Running two instances on the same session can cause conflicts. "
                f"Consider using `cfork` (fork-resume) or `cread` (read-only fork) instead. "
                f"Dismiss this warning to continue anyway."
            )
            print(json.dumps({"decision": "block", "reason": reason}))
            # Don't overwrite the lock — the original session is still active.
            # If the user dismisses, they accept the risk of two instances.
            return

    # No conflict — write our lock
    lock_file.write_text(str(claude_pid))


if __name__ == '__main__':
    main()
