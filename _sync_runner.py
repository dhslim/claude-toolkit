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
IS_WINDOWS = sys.platform == 'win32'

sys.path.insert(0, str(SCRIPT_DIR))


def _acquire_lock(lock_fd):
    """Acquire exclusive lock on lock_fd. Returns True on success."""
    try:
        if IS_WINDOWS:
            import msvcrt
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, PermissionError, BlockingIOError):
        return False


def _release_lock(lock_fd):
    """Release lock on lock_fd. Silently ignore errors."""
    try:
        if IS_WINDOWS:
            import msvcrt
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def log(msg):
    """Append to sync.log using a lockfile to prevent corruption."""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}\n'
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, 'w')
        _acquire_lock(lock_fd)
    except (OSError, PermissionError):
        # If we can't open the lockfile, write the log anyway — better than losing it
        pass
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    finally:
        if lock_fd:
            _release_lock(lock_fd)
            lock_fd.close()


def _diag_write(line: str) -> None:
    """Write directly to sync.log with no locking, no imports — for diagnostics.

    Used to record that the child process started AT ALL, before any code
    path that could fail (imports, locking, MongoDB connect, etc).
    """
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def main():
    # Diagnostic: prove the child process actually started.
    # If this line is missing from sync.log, subprocess.Popen never spawned us.
    import os
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    _diag_write(f'[{ts}] CHILD STARTED pid={os.getpid()} platform={sys.platform} argv={sys.argv[1:]}\n')

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
