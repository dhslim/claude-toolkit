#!/usr/bin/env python3
"""Fetch conversations for a given date and output them for quiz generation.

Quiz question generation is handled by Claude Code itself (via the SessionStart
hook flow), not by calling the Anthropic API directly. This script just fetches
and formats the conversation data.

Usage: python daily_quiz.py [--date 2026-03-17]
"""

import argparse
import io
import sys
from datetime import datetime, timedelta, timezone

from _shared import get_db, now_kst, KST

# Ensure stdout is UTF-8 on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def parse_args():
    parser = argparse.ArgumentParser(description='Fetch conversation data for quiz generation')
    parser.add_argument('--date', help='Date to fetch (YYYY-MM-DD, default: yesterday)')
    args = parser.parse_args()

    if not args.date:
        yesterday = now_kst() - timedelta(days=1)
        args.date = yesterday.strftime('%Y-%m-%d')
    return args


def extract_content(sessions):
    """Extract quiz-relevant content from sessions (up to 200K chars)."""
    chunks = []
    total_chars = 0
    max_chars = 200000

    for session in sessions:
        if total_chars >= max_chars:
            break
        date_str = ''
        sd = session.get('session_date')
        if sd and hasattr(sd, 'strftime'):
            date_str = sd.strftime('%Y-%m-%d')
        chunks.append(f'\n--- Session: {session.get("project", "?")} ({date_str}) ---\n')

        for msg in session.get('messages') or []:
            if total_chars >= max_chars:
                break
            if msg.get('role') not in ('user', 'assistant'):
                continue

            content = msg.get('content', '')
            text = ''
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = '\n'.join(
                    b.get('text', '') for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                )

            if text:
                prefix = 'User' if msg['role'] == 'user' else 'Claude'
                truncated = text[:2000]
                chunks.append(f'[{prefix}]: {truncated}')
                total_chars += len(truncated) + 20

    return '\n'.join(chunks)


def main():
    args = parse_args()
    date = args.date
    print(f'Fetching conversations from {date}...', file=sys.stderr)

    client, db = get_db()
    try:
        start_dt = datetime.fromisoformat(f'{date}T00:00:00').replace(tzinfo=KST)
        end_dt = datetime.fromisoformat(f'{date}T23:59:59').replace(tzinfo=KST)

        sessions = list(db['sessions'].find({
            'session_date': {'$gte': start_dt, '$lte': end_dt}
        }))

        if not sessions:
            print(f'No sessions found for {date}')
            sys.exit(0)

        print(f'Found {len(sessions)} sessions', file=sys.stderr)

        content = extract_content(sessions)
        if len(content) < 100:
            print('Conversation content too short for quiz generation')
            sys.exit(0)

        print(f'Extracted {len(content) / 1000:.1f}K characters', file=sys.stderr)

        # Output content to stdout for Claude Code to consume
        print(content)

    except Exception as e:
        print(f'Failed to fetch data: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
