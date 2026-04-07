#!/usr/bin/env python3
"""Fetch yesterday's conversation sessions from MongoDB for quiz generation."""

import io
import sys
from datetime import timedelta, timezone

# Ensure stdout is UTF-8 on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from _shared import get_db, now_kst


def main():
    now = now_kst()
    yesterday = now - timedelta(days=1)
    start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Convert KST boundaries to UTC for message timestamp comparison
    start_utc = start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    end_utc = end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    client, db = get_db()
    try:
        # Find sessions that have at least one message within yesterday (KST).
        # session_date is session start time, but messages have their own timestamps.
        # A session started days ago may still have messages from yesterday.
        # We cast a wider net on sessions, then filter messages by timestamp.
        three_days_ago = now - timedelta(days=3)
        td_start = three_days_ago.replace(hour=0, minute=0, second=0, microsecond=0)
        sessions = list(db['sessions'].find({
            'session_date': {'$gte': td_start, '$lte': end}
        }))

        if not sessions:
            print('No recent sessions found for quiz generation.')
            sys.exit(0)

        # Extract conversation content — only messages whose timestamp falls within yesterday (KST)
        max_chars = 200000
        all_chunks = []
        total_msg_count = 0

        for session in sessions:
            device = session.get('device', '?')
            project = session.get('project', '?')
            session_chunks = []
            session_chars = 0

            for msg in session.get('messages') or []:
                if msg.get('role') not in ('user', 'assistant'):
                    continue

                # Filter by message timestamp (ISO 8601 string, UTC)
                ts = msg.get('timestamp', '')
                if not ts or ts < start_utc or ts > end_utc:
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
                    session_chunks.append(f'[{prefix}]: {truncated}')
                    session_chars += len(truncated)
                    total_msg_count += 1

            # Only include sessions that had messages within yesterday
            if session_chunks:
                header = f'\n--- Session: {project} [{device}] ---\n'
                all_chunks.append(header)
                all_chunks.extend(session_chunks)

        # Budget enforcement: trim to max_chars
        output = '\n'.join(all_chunks)
        if len(output) > max_chars:
            output = output[:max_chars]

        session_count = sum(1 for c in all_chunks if c.startswith('\n--- Session'))
        print(f'Found {total_msg_count} messages from yesterday across {session_count} sessions.')
        print(output)

    except Exception as e:
        print(f'Failed to fetch quiz data: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
