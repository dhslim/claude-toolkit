#!/usr/bin/env python3
"""Mark today's quiz as completed — writes to MongoDB (global) + local file (cache)."""

from datetime import datetime, timezone
from pathlib import Path
from _shared import today_kst, get_db, to_kst_iso

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_FILE = SCRIPT_DIR / 'quiz-last-taken.txt'

today = today_kst()

# Write to MongoDB (source of truth, shared across machines)
try:
    client, db = get_db()
    now_utc = datetime.now(timezone.utc)
    db['quiz-markers'].update_one(
        {'date': today},
        {'$set': {'taken_at': now_utc, 'taken_at_kst': to_kst_iso(now_utc)}},
        upsert=True
    )
    client.close()
except Exception as e:
    print(f'Warning: MongoDB write failed ({e}), local-only')

# Write local cache
LOCAL_FILE.write_text(today, encoding='utf-8')
print(f'Quiz marked as done for {today}')
