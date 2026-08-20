"""Shared utilities for conversation-warehouse scripts."""

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import (
    AutoReconnect, ConnectionFailure, ServerSelectionTimeoutError,
    NetworkTimeout, WaitQueueTimeoutError
)

# Load .env from this script's directory
_SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(_SCRIPT_DIR / '.env')

# KST timezone (UTC+9, no DST in Korea)
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


def today_kst():
    return now_kst().strftime('%Y-%m-%d')


def to_kst_iso(dt):
    """Convert a UTC datetime (aware or naive UTC) to an ISO-8601 string in KST with +09:00 offset.

    Used to write `*_kst` companion fields alongside canonical UTC datetimes in MongoDB.
    Storing KST as an explicit-offset string (not a fake-UTC datetime) keeps it
    self-describing and prevents silent timezone coercion by BSON.
    """
    if dt is None:
        return None
    # If it's a string (e.g. message timestamps from JSONL like "2026-05-18T21:46:13.186Z"), parse first.
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).isoformat()


def get_db():
    """Return (client, db) tuple for conversation-warehouse database."""
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        raise RuntimeError('MONGODB_URI environment variable not set')
    client = MongoClient(uri,
                         serverSelectionTimeoutMS=10000,
                         connectTimeoutMS=10000,
                         socketTimeoutMS=120000,
                         retryWrites=True,
                         retryReads=True)
    db = client['conversation-warehouse']
    return client, db


# Transient MongoDB errors worth retrying
_TRANSIENT_ERRORS = (AutoReconnect, ConnectionFailure,
                     ServerSelectionTimeoutError, NetworkTimeout,
                     WaitQueueTimeoutError)


def get_db_fast(timeout_ms=5000):
    """Like get_db() but with aggressive timeouts for time-critical hooks."""
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        raise RuntimeError('MONGODB_URI environment variable not set')
    client = MongoClient(uri,
                         serverSelectionTimeoutMS=timeout_ms,
                         connectTimeoutMS=timeout_ms,
                         socketTimeoutMS=timeout_ms,
                         retryWrites=False,
                         retryReads=False)
    db = client['conversation-warehouse']
    return client, db


def with_retry(fn, max_retries=3):
    """Call fn() with exponential backoff on transient MongoDB errors."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except _TRANSIENT_ERRORS:
            if attempt == max_retries:
                raise
            time.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s


def yesterday_kst_bounds_utc():
    """Return (start_utc, end_utc) ISO-8601 strings for yesterday's KST calendar
    day as a half-open window [start, end).

    The single definition of "yesterday" shared by the quiz scripts. Boundaries
    are converted to UTC because message `timestamp` fields are stored in UTC
    (ISO-8601 strings, which sort lexicographically).
    """
    start = (now_kst() - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    fmt = '%Y-%m-%dT%H:%M:%S'
    return (start.astimezone(timezone.utc).strftime(fmt),
            end.astimezone(timezone.utc).strftime(fmt))


def count_quiz_messages(db, start_utc, end_utc):
    """Count user/assistant messages whose timestamp is in [start_utc, end_utc),
    across all sessions, entirely server-side. Used to decide whether a day had
    any quiz-worthy activity (0 => nothing to quiz on)."""
    pipeline = [
        {'$match': {'messages.timestamp': {'$gte': start_utc, '$lt': end_utc}}},
        {'$unwind': '$messages'},
        {'$match': {'messages.timestamp': {'$gte': start_utc, '$lt': end_utc},
                    'messages.role': {'$in': ['user', 'assistant']}}},
        {'$count': 'n'},
    ]
    res = list(db['sessions'].aggregate(pipeline))
    return res[0]['n'] if res else 0


def force_utf8_io(strict_stdin=False):
    r"""Pin this process's stdout/stderr to UTF-8, whatever the locale says.

    Any script whose output is consumed by an agent MUST call this before
    printing non-ASCII, and pair it with json.dumps(..., ensure_ascii=False).
    The two go together:

      * ensure_ascii=True (the default) escapes Korean to \uXXXX. The text
        survives a json.loads round-trip but is invisible to a plain grep --
        a search for the escaped form returns 0 while the data sits right
        there. mongo_recent.py shipped that way and a /mgo lookup was read as
        "not synced" for a full day (4dafcb8).
      * dropping the escapes without pinning stdout is WORSE: stdout here
        follows the Windows console codepage (cp949 on this box), and cp949
        bytes read back as UTF-8 become U+FFFD -- unsearchable AND unrecoverable.

    cp949 is not the villain; it encodes Korean fine. The failure is a writer
    and a reader disagreeing about which alphabet the bytes are in, so state it
    explicitly instead of inheriting whatever the console happened to be.

    errors='replace' on output so an unencodable stray can never crash a report.
    strict_stdin=True leaves stdin STRICT, so a non-UTF-8 payload fails loudly
    rather than being persisted with holes punched through it.
    """
    for stream, errors in ((sys.stdout, 'replace'), (sys.stderr, 'replace')):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors=errors)
    if strict_stdin and hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8', errors='strict')
