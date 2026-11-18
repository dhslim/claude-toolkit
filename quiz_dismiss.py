#!/usr/bin/env python3
"""Dismiss today's quiz — writes to MongoDB (global) + local file (cache)."""

from datetime import datetime, timezone
from pathlib import Path
from _shared import today_kst, get_db, to_kst_iso

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_FILE = SCRIPT_DIR / 'quiz-last-dismissed.txt'
INSTRUCTIONS_FILE = SCRIPT_DIR / 'quiz-instructions-active.txt'

today = today_kst()

# Write to MongoDB (source of truth, shared across machines)
try:
    client, db = get_db()
    now_utc = datetime.now(timezone.utc)
    db['quiz-markers'].update_one(
        {'date': today},
        {'$set': {'dismissed_at': now_utc, 'dismissed_at_kst': to_kst_iso(now_utc)}},
        upsert=True
    )
    client.close()
except Exception as e:
    print(f'Warning: MongoDB write failed ({e}), local-only')

# Write local cache
LOCAL_FILE.write_text(today, encoding='utf-8')

# Clear the active instructions so a superseded directive cannot outlive the
# decision that superseded it. quiz_check.py regenerates this file the next time
# it decides to nag, so removing it loses nothing. Leaving it in place is what
# let a four-day-old "present THIS quiz" directive keep circulating on
# 2026-08-18 — instructions an agent may still be holding in context.
try:
    INSTRUCTIONS_FILE.unlink(missing_ok=True)
except OSError as e:
    print(f'Warning: could not remove {INSTRUCTIONS_FILE.name} ({e})')

print(f'Quiz dismissed for {today}')
