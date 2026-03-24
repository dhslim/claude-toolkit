#!/usr/bin/env python3
"""SessionEnd hook — removes the session lock file created by hook_session_guard.py.

Only removes the lock if the PID in the file matches our own claude ancestor.
This prevents cfork/cread sessions from deleting the original session's lock.
"""

import json
import os
import sys
from pathlib import Path

LOCK_DIR = Path.home() / '.claude' / 'session-locks'


def _find_claude_ancestor() -> int | None:
    """Walk up the process tree to find the claude process."""
    try:
        pid = os.getpid()
        if Path('/proc/self/stat').exists():
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
