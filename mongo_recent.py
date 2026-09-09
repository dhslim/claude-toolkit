#!/usr/bin/env python3
"""Fetch recent Claude Code activity from MongoDB across all machines.

Uses an aggregation pipeline to filter messages SERVER-SIDE rather than
pulling full session documents (often MB each) and filtering in Python.
For a typical 30m window the payload drops from ~5 MB to ~10 KB, which
matters on higher-RTT network paths where the larger transfer can blow
past pymongo's default socketTimeoutMS.

Usage: mongo_recent.py <duration>
  10    → last 10 minutes (default unit)
  30m   → last 30 minutes
  2h    → last 2 hours
  1d    → last 1 day

Note: this is a ROLLING window from `datetime.now(UTC) - duration`, not
a calendar-day cutoff. `/mongo 1d` run at 10:00 KST returns everything
since 10:00 KST yesterday — NOT everything since 00:00 KST yesterday.
For use cases that expect "yesterday's activity" as a calendar day
(e.g. daily quiz), this means late-night activity from 2 days ago may
still appear and early-morning activity from yesterday may be missing.
Left as rolling-window by design (2026-04); revisit if calendar-day
semantics become important.
"""
from __future__ import annotations

import json
import io
import re
import sys
from datetime import datetime, timedelta, timezone

from _shared import get_db


def _rewrap(stream, encoding, errors):
    """Pin one text stream to `encoding`, whatever the console/locale says.

    reconfigure() is the 3.7+ way; the TextIOWrapper rebuild is the fallback for
    a stream that lacks it. Same helper as quiz_save.py:27.
    """
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding=encoding, errors=errors)
        return stream
    if hasattr(stream, 'buffer'):
        return io.TextIOWrapper(stream.buffer, encoding=encoding, errors=errors)
    return stream


def force_utf8_stdout():
    """Emit UTF-8 regardless of the console codepage.

    Required by the ensure_ascii=False below. This warehouse is Korean-heavy and
    locale.getpreferredencoding(False) is cp949 on this box, so an unconfigured
    stdout renders 배기원 as mojibake the moment the escapes are removed. The
    /slack-style callers invoke this script bare, without PYTHONIOENCODING, so
    the script guarantees its own encoding rather than relying on every caller
    to remember (the reasoning quiz_save.py:42 spells out at length).

    errors='replace' so a stray unencodable character can never crash a report.
    """
    _rewrap(sys.stdout, 'utf-8', 'replace')
    _rewrap(sys.stderr, 'utf-8', 'replace')


def parse_duration(arg: str) -> timedelta:
    """Parse duration string into timedelta. Default unit is minutes."""
    arg = arg.strip().lower()
    match = re.match(r'^(\d+)\s*([mhd])?$', arg)
    if not match:
        print(f'Invalid duration: {arg}', file=sys.stderr)
        print('Examples: 10 (10 min), 30m, 2h, 1d', file=sys.stderr)
        sys.exit(1)

    value = int(match.group(1))
    unit = match.group(2) or 'm'

    if unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)


def extract_text(content) -> str:
    """Extract text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
        return '\n'.join(parts)
    return ''


def main():
    force_utf8_stdout()
    if len(sys.argv) < 2:
        print('Usage: mongo_recent.py <duration>', file=sys.stderr)
        print('Examples: 10 (10 min), 30m, 2h, 1d', file=sys.stderr)
        sys.exit(1)

    duration = parse_duration(sys.argv[1])
    cutoff = datetime.now(timezone.utc) - duration

    client, db = get_db()
    db['sessions'].create_index('last_synced_at')

    # Aggregation pipeline: filter sessions + filter messages server-side
    pipeline = [
        {'$match': {'last_synced_at': {'$gte': cutoff}}},
        {'$sort': {'last_synced_at': -1}},
        {'$project': {
            'session_id': 1,
            'session_name': 1,
            'project': 1,
            'device': 1,
            'last_synced_at': 1,
            'last_synced_at_kst': 1,
            'messages': {
                '$filter': {
                    'input': {'$ifNull': ['$messages', []]},
                    'as': 'm',
                    'cond': {
                        # Match V1: timestamp-only filter (role filtering
                        # happens in Python to preserve recent_message_count
                        # semantics — V1 counts all in-window messages
                        # incl. attachments/permission-mode records)
                        '$gte': [
                            {'$dateFromString': {
                                'dateString': '$$m.timestamp',
                                'onError': None
                            }},
                            cutoff
                        ]
                    }
                }
            }
        }}
    ]
    sessions = list(db['sessions'].aggregate(pipeline))

    # Instrumentation: split the funnel so a future "0 results" is self-diagnosing.
    #   matched_by_last_synced_at = sessions whose last_synced_at is in-window ($match)
    #   dropped_no_inwindow_msgs  = of those, how many carried ZERO in-window messages
    #                               after the server-side timestamp $filter (synced
    #                               recently but nothing fresh, OR message timestamps
    #                               failed $dateFromString parsing).
    # A future 0 then tells us instantly: matched==0 -> $match/clock/sync/read issue;
    # matched>0 but all dropped -> message-timestamp filter issue.
    matched_by_last_synced_at = len(sessions)
    dropped_no_inwindow_msgs = 0

    results = []
    for session in sessions:
        # Server already filtered to in-window user/assistant messages.
        recent_msgs = session.get('messages') or []
        if not recent_msgs:
            dropped_no_inwindow_msgs += 1
            continue

        # Extract user and assistant messages (same logic as v1)
        user_msgs = []
        assistant_msgs = []
        for msg in recent_msgs:
            text = extract_text(msg.get('content', ''))
            if not text or '<local-command' in text:
                continue
            if msg.get('role') == 'user':
                user_msgs.append(text)
            elif msg.get('role') == 'assistant':
                assistant_msgs.append(text)

        results.append({
            'session_id': session.get('session_id', '?')[:8],
            'session_name': session.get('session_name') or '(unnamed)',
            'project': session.get('project', '?'),
            'device': session.get('device', '?'),
            'last_synced_at': str(session.get('last_synced_at', '?')),
            'last_synced_at_kst': session.get('last_synced_at_kst', '?'),
            'recent_message_count': len(recent_msgs),
            'user_messages': user_msgs,
            'assistant_messages': assistant_msgs,
        })

    client.close()

    # ensure_ascii=False: the default escapes Korean to \uXXXX, which made
    # every /mgo output unsearchable by any Korean term -- a grep for the
    # manager's name returned 0 while the file held 25 escaped occurrences.
    print(json.dumps({
        'query': {
            'duration': sys.argv[1],
            'cutoff_utc': cutoff.isoformat(),
            'sessions_found': len(results),
            'matched_by_last_synced_at': matched_by_last_synced_at,
            'dropped_no_inwindow_msgs': dropped_no_inwindow_msgs,
        },
        'sessions': results,
    }, indent=2, default=str, ensure_ascii=False))


if __name__ == '__main__':
    main()
