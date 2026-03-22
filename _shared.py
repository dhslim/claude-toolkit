"""Shared utilities for conversation-warehouse scripts."""

import os
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


def get_db():
    """Return (client, db) tuple for conversation-warehouse database."""
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        raise RuntimeError('MONGODB_URI environment variable not set')
    client = MongoClient(uri,
                         serverSelectionTimeoutMS=10000,
                         connectTimeoutMS=10000,
                         socketTimeoutMS=30000,
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
