#!/usr/bin/env python3
"""Hook wrapper — reads stdin JSON, spawns detached sync for a single file.

Used by Stop and SessionEnd hooks. The child process is fully detached
so the parent hook can exit without waiting for sync to complete.

The child runs via _sync_runner.py which catches errors and logs them
with proper file locking to avoid concurrent write corruption.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / 'sync.log'
RUNNER_SCRIPT = SCRIPT_DIR / '_sync_runner.py'
KST = timezone(timedelta(hours=9))

# Determine venv python path
if sys.platform == 'win32':
    PYTHON = str(SCRIPT_DIR / '.venv' / 'Scripts' / 'pythonw.exe')
else:
    PYTHON = str(SCRIPT_DIR / '.venv' / 'bin' / 'python')


def _log_ts():
    """Dual KST | UTC timestamp prefix to avoid timezone confusion when reading sync.log."""
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(KST)
    return f"{now_kst.strftime('%Y-%m-%d %H:%M:%S')} KST | {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def log(msg):
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f'[{_log_ts()}] {msg}\n')


def main():
    data = sys.stdin.read().strip()
    try:
        hook = json.loads(data)
        tp = hook.get('transcript_path')
        event = hook.get('hook_event_name', 'unknown')
        sid = (hook.get('session_id') or '?')[:8]

        if tp:
            kwargs = {
                'stdin': subprocess.DEVNULL,
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL,
            }

            if sys.platform == 'win32':
                DETACHED = 0x00000008  # DETACHED_PROCESS
                CREATE_NO_WINDOW = 0x08000000
                kwargs['creationflags'] = DETACHED | CREATE_NO_WINDOW
            else:
                kwargs['start_new_session'] = True

            proc = subprocess.Popen(
                [PYTHON, str(RUNNER_SCRIPT), '--file', tp, '--sid', sid],
                **kwargs,
            )
            # Don't wait for child
            log(f'{event} -> {sid} sync started (pid {proc.pid})')
    except Exception as e:
        log(f'ERROR: {e}')


if __name__ == '__main__':
    main()
