#!/usr/bin/env python3
"""SessionStart hook — warns if another Claude Code instance is already using
the same session ID (e.g. opened in two terminals).

Writes a PID lock file per session. If a lock already exists with a live
process, emits a blocking warning so the user can dismiss or switch to
cfork/cread instead.
"""

import json
import os
import signal
import sys
from pathlib import Path

LOCK_DIR = Path.home() / '.claude' / 'session-locks'


def _pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but we can't signal it — still alive
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _get_process_tty(pid: int) -> str:
    """Try to get the TTY of a process for a more helpful warning."""
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
    my_pid = os.getpid()
    # The parent claude process is what we actually want to track
    parent_pid = os.getppid()

    # Check for existing lock
    if lock_file.exists():
        try:
            existing_pid = int(lock_file.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid and existing_pid != parent_pid and _pid_alive(existing_pid):
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
            # Still write our PID — if the user dismisses, we're the active one now
            lock_file.write_text(str(parent_pid))
            return

    # No conflict — write our lock
    lock_file.write_text(str(parent_pid))


if __name__ == '__main__':
    main()
