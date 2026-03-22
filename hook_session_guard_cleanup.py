#!/usr/bin/env python3
"""SessionEnd hook — removes the session lock file created by hook_session_guard.py."""

import json
import sys
from pathlib import Path

LOCK_DIR = Path.home() / '.claude' / 'session-locks'


def main():
    try:
        hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    session_id = hook_data.get('session_id')
    if not session_id:
        return

    lock_file = LOCK_DIR / f'{session_id}.pid'
    try:
        lock_file.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == '__main__':
    main()
