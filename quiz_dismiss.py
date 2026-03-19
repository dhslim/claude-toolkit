#!/usr/bin/env python3
"""Dismiss today's quiz — writes to MongoDB (global) + local file (cache)."""

from datetime import datetime, timezone
from pathlib import Path
from _shared import today_kst, get_db

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_FILE = SCRIPT_DIR / 'quiz-last-dismissed.txt'

today = today_kst()

# Write to MongoDB (source of truth, shared across machines)
try:
    client, db = get_db()
    db['quiz-markers'].update_one(
        {'date': today},
        {'$set': {'dismissed_at': datetime.now(timezone.utc)}},
        upsert=True
    )
    client.close()
except Exception as e:
    print(f'Warning: MongoDB write failed ({e}), local-only')

# Write local cache
LOCAL_FILE.write_text(today, encoding='utf-8')
print(f'Quiz dismissed for {today}')
