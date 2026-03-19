#!/usr/bin/env python3
"""Hook wrapper — reads stdin JSON, spawns detached sync for a single file.

Used by Stop and SessionEnd hooks. The child process is fully detached
so the parent hook can exit without waiting for sync to complete.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / 'sync.log'
SYNC_SCRIPT = SCRIPT_DIR / 'sync_conversations.py'

# Determine venv python path
if sys.platform == 'win32':
    PYTHON = str(SCRIPT_DIR / '.venv' / 'Scripts' / 'python.exe')
else:
    PYTHON = str(SCRIPT_DIR / '.venv' / 'bin' / 'python')


def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f'[{ts}] {msg}\n')


def main():
    data = sys.stdin.read().strip()
    try:
        hook = json.loads(data)
        tp = hook.get('transcript_path')
        event = hook.get('hook_event_name', 'unknown')
        sid = (hook.get('session_id') or '?')[:8]

        if tp:
            log_fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND)

            kwargs = {
                'stdin': subprocess.DEVNULL,
                'stdout': log_fd,
                'stderr': log_fd,
            }

            if sys.platform == 'win32':
                DETACHED = 0x00000008  # DETACHED_PROCESS
                CREATE_NO_WINDOW = 0x08000000
                kwargs['creationflags'] = DETACHED | CREATE_NO_WINDOW
            else:
                kwargs['start_new_session'] = True

            proc = subprocess.Popen(
                [PYTHON, str(SYNC_SCRIPT), '--file', tp],
                **kwargs,
            )
            # Don't wait for child
            log(f'{event} -> {sid} sync started (pid {proc.pid})')
            os.close(log_fd)
    except Exception as e:
        log(f'ERROR: {e}')


if __name__ == '__main__':
    main()
