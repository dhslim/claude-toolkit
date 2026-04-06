#!/usr/bin/env python3
"""Child process that runs sync_conversations for a single file.

Spawned by hook_sync.py as a detached process. Handles its own error
logging with file locking so concurrent runners don't corrupt the log.
"""

import argparse
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / 'sync.log'
LOCK_FILE = SCRIPT_DIR / 'sync.log.lock'

sys.path.insert(0, str(SCRIPT_DIR))


def log(msg):
    """Append to sync.log using a lockfile to prevent corruption."""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}\n'
    import msvcrt
    lock_fd = None
    try:
        # Acquire exclusive lock via lockfile
        lock_fd = open(LOCK_FILE, 'w')
        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, PermissionError):
        # If we can't lock, write anyway — better than losing the log
        pass
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    finally:
        if lock_fd:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            lock_fd.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', required=True)
    parser.add_argument('--sid', default='?')
    args = parser.parse_args()

    sid = args.sid
    file_path = Path(args.file)

    try:
        if not file_path.exists():
            log(f'SYNC FAIL {sid}: file not found: {file_path}')
            return

        from sync_conversations import sync_one_file
        from _shared import get_db

        client, db = get_db()
        try:
            collection = db['sessions']
            result = sync_one_file(collection, file_path)
            if result:
                log(f'SYNC OK   {sid}: {result["action"]} ({result["message_count"]} msgs)')
            else:
                log(f'SYNC SKIP {sid}: no session_id or messages')
        finally:
            client.close()

    except Exception:
        tb = traceback.format_exc().replace('\n', ' | ')
        log(f'SYNC ERR  {sid}: {tb}')


if __name__ == '__main__':
    main()
