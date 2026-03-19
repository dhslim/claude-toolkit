#!/usr/bin/env python3
"""Fetch yesterday's conversation sessions from MongoDB for quiz generation."""

import io
import sys
from datetime import timedelta

# Ensure stdout is UTF-8 on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from _shared import get_db, now_kst


def main():
    now = now_kst()
    yesterday = now - timedelta(days=1)
    y_str = yesterday.strftime('%Y-%m-%d')
    start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)

    client, db = get_db()
    try:
        sessions = list(db['sessions'].find({
            'session_date': {'$gte': start, '$lte': end}
        }))

        if not sessions:
            # Fall back to last 3 days
            three_days_ago = now - timedelta(days=3)
            td_start = three_days_ago.replace(hour=0, minute=0, second=0, microsecond=0)
            sessions = list(db['sessions'].find({
                'session_date': {'$gte': td_start, '$lte': end}
            }).sort('session_date', -1).limit(5))

            if not sessions:
                print('No recent sessions found for quiz generation.')
                sys.exit(0)

        # Extract conversation content (user/assistant text only, 50K char limit)
        chunks = []
        total_chars = 0
        max_chars = 50000

        for session in sessions:
            if total_chars >= max_chars:
                break
            date_str = ''
            sd = session.get('session_date')
            if sd:
                date_str = sd.strftime('%Y-%m-%d') if hasattr(sd, 'strftime') else str(sd)[:10]
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
                    truncated = text[:1000]
                    chunks.append(f'[{prefix}]: {truncated}')
                    total_chars += len(truncated)

        print(f'Found {len(sessions)} sessions from yesterday.')
        print('\n'.join(chunks))

    except Exception as e:
        print(f'Failed to fetch quiz data: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
