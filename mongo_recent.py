#!/usr/bin/env python3
"""Fetch recent Claude Code activity from MongoDB across all machines.

Usage: mongo_recent.py <duration>
  10    → last 10 minutes (default unit)
  30m   → last 30 minutes
  2h    → last 2 hours
  1d    → last 1 day
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

from _shared import get_db


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
    if len(sys.argv) < 2:
        print('Usage: mongo_recent.py <duration>', file=sys.stderr)
        print('Examples: 10 (10 min), 30m, 2h, 1d', file=sys.stderr)
        sys.exit(1)

    duration = parse_duration(sys.argv[1])
    cutoff = datetime.now(timezone.utc) - duration

    client, db = get_db()
    # synced_at 인덱스 확인 및 생성 (sort 메모리 제한 방지)
    db['sessions'].create_index('synced_at')
    sessions = list(db['sessions'].find({
        'synced_at': {'$gte': cutoff}
    }).sort('synced_at', -1))

    results = []
    for session in sessions:
        # Filter messages within the time window
        recent_msgs = []
        for msg in session.get('messages') or []:
            ts_str = msg.get('timestamp')
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts >= cutoff:
                    recent_msgs.append(msg)
            except (ValueError, TypeError):
                continue

        if not recent_msgs:
            continue

        # Extract user and assistant messages
        user_msgs = []
        assistant_msgs = []
        for msg in recent_msgs:
            text = extract_text(msg.get('content', ''))
            if not text or '<local-command' in text:
                continue
            text = text[:500]  # truncate long messages
            if msg.get('role') == 'user':
                user_msgs.append(text)
            elif msg.get('role') == 'assistant':
                assistant_msgs.append(text)

        results.append({
            'session_id': session.get('session_id', '?')[:8],
            'session_name': session.get('session_name') or '(unnamed)',
            'project': session.get('project', '?'),
            'device': session.get('device', '?'),
            'synced_at': str(session.get('synced_at', '?')),
            'recent_message_count': len(recent_msgs),
            'user_messages': user_msgs[:20],
            'assistant_messages': assistant_msgs[:20],
        })

    client.close()

    print(json.dumps({
        'query': {
            'duration': sys.argv[1],
            'cutoff_utc': cutoff.isoformat(),
            'sessions_found': len(results),
        },
        'sessions': results,
    }, indent=2, default=str))


if __name__ == '__main__':
    main()
