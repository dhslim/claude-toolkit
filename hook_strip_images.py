#!/usr/bin/env python3
"""SessionEnd hook — strip image blocks from the session JSONL.

Spawns a detached worker that waits for `_sync_runner.py` to finish (by tailing
sync.log for this session's completion marker), then invokes
strip_session_images.py to clean the JSONL in place.

Order of events on SessionEnd:
  1. hook_sync.py      — spawns detached sync_runner (uploads to MongoDB)
  2. hook_strip_images — spawns detached strip worker (waits for sync log marker,
                         then strips)

The strip worker never edits the file until it has confirmation that the
archive copy was written, so MongoDB always has the full image-containing
version even after local JSONL is stripped.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / 'strip.log'
STRIP_SCRIPT = SCRIPT_DIR / 'strip_session_images.py'

if sys.platform == 'win32':
    PYTHON = str(SCRIPT_DIR / '.venv' / 'Scripts' / 'pythonw.exe')
else:
    PYTHON = str(SCRIPT_DIR / '.venv' / 'bin' / 'python')


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f'[{ts}] {msg}\n')


def main():
    try:
        data = sys.stdin.read().strip()
        hook = json.loads(data) if data else {}
    except (json.JSONDecodeError, OSError) as e:
        log(f'ERROR bad stdin: {e}')
        return

    tp = hook.get('transcript_path')
    event = hook.get('hook_event_name', 'unknown')
    sid = (hook.get('session_id') or '?')[:8]

    if not tp:
        log(f'{event} {sid} skip: no transcript_path')
        return
    if not Path(tp).exists():
        log(f'{event} {sid} skip: file not found: {tp}')
        return

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

    args = [
        PYTHON, str(STRIP_SCRIPT), tp,
        '--skip-if-clean', '--no-backup', '--quiet',
        '--wait-for-sync', sid,
    ]
    try:
        proc = subprocess.Popen(args, **kwargs)
        log(f'{event} {sid} strip worker started (pid {proc.pid})')
    except Exception as e:
        log(f'{event} {sid} ERR spawning worker: {e}')


if __name__ == '__main__':
    main()
