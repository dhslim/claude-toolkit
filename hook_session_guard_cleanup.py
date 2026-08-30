#!/usr/bin/env python3
"""SessionEnd hook — removes the session lock file created by hook_session_guard.py.

Only removes the lock if the PID in the file matches our own claude ancestor.
This prevents cfork/cread sessions from deleting the original session's lock.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Optional

IS_WINDOWS = platform.system() == 'Windows'

LOCK_DIR = Path.home() / '.claude' / 'session-locks'

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes

    TH32CS_SNAPPROCESS = 0x00000002
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
        """Get (parent_pid, exe_name) for a given PID using snapshot."""
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


def _find_claude_ancestor() -> Optional[int]:
    """Walk up the process tree to find the claude process."""
    try:
        pid = os.getpid()
        if IS_WINDOWS:
            for _ in range(15):
                ppid, name = _win_get_process_info(pid)
                if ppid is None or ppid <= 1:
                    break
                if name == 'claude.exe':
                    return ppid
                pid = ppid
        elif Path('/proc/self/stat').exists():
            for _ in range(10):
                pid = int(Path(f'/proc/{pid}/stat').read_text().split()[3])
                if pid <= 1:
                    break
                try:
                    cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
                    cmd = cmdline.split(b'\x00')[0].decode()
                    if cmd.endswith('/claude') or cmd == 'claude':
                        return pid
                except (OSError, UnicodeDecodeError):
                    continue
        else:
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
                    return ppid
                pid = ppid
    except Exception:
        pass
    return None


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    session_id = hook_data.get('session_id')
    if not session_id:
        return

    lock_file = LOCK_DIR / f'{session_id}.pid'
    if not lock_file.exists():
        return

    # Only delete if the lock belongs to us
    claude_pid = _find_claude_ancestor() or os.getppid()
    try:
        existing_pid = int(lock_file.read_text().strip())
    except (ValueError, OSError):
        existing_pid = None

    if existing_pid == claude_pid:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == '__main__':
    main()
