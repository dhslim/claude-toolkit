#!/usr/bin/env python3
"""Fetch yesterday's conversation messages from MongoDB for quiz generation.

Filtering is done SERVER-SIDE by an aggregation pipeline that matches on each
MESSAGE's timestamp — not on session_started_at. A long-running session that
started weeks ago but was active yesterday is therefore included (the old
session-level pre-filter silently dropped those — ~86% of real activity on a
typical day). Only the in-window user/assistant messages cross the wire; the DB
does the work, not the client.

Ordering note: the pipeline deliberately has NO $sort stages. Sorting unwound
messages (or the regrouped session docs) server-side carries the full message
content through the sort and blows past MongoDB's 32MB in-memory sort limit
(error 292 / QueryExceededMemoryLimitNoDiskUseAllowed), and allowDiskUse is not
permitted on shared Atlas tiers. Each message's timestamp is carried through the
$group instead, and both orderings — chronological within a session, and
sessions by id — are applied client-side below, where the data is already
truncated and small.
"""

import io
import sys

# Ensure stdout is UTF-8 on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from _shared import get_db, yesterday_kst_bounds_utc

MAX_CHARS = 200000      # total output budget
MAX_MSG_CHARS = 1000    # per-message truncation


def main():
    # Half-open KST day window [yesterday 00:00, today 00:00) as UTC strings,
    # matched against the per-message UTC `timestamp` (ISO-8601, sorts lexically).
    start_utc, end_utc = yesterday_kst_bounds_utc()

    client, db = get_db()
    try:
        pipeline = [
            # 1) sessions with >=1 message in yesterday's window (NO session_started_at gate)
            {'$match': {'messages.timestamp': {'$gte': start_utc, '$lt': end_utc}}},
            # 2) explode to one doc per message
            {'$unwind': '$messages'},
            # 3) keep only the in-window user/assistant messages
            {'$match': {
                'messages.timestamp': {'$gte': start_utc, '$lt': end_utc},
                'messages.role': {'$in': ['user', 'assistant']},
            }},
            # 4) regroup by session for presentation; timestamp is kept so the
            #    chronological ordering can be applied client-side (see docstring)
            {'$group': {
                '_id': '$session_id',
                'project': {'$first': '$project'},
                'device': {'$first': '$device'},
                'messages': {'$push': {
                    'role': '$messages.role',
                    'content': '$messages.content',
                    'timestamp': '$messages.timestamp',
                }},
            }},
        ]

        groups = list(db['sessions'].aggregate(pipeline))
        if not groups:
            print('No messages found for yesterday for quiz generation.')
            sys.exit(0)

        # Sessions by id (was the trailing server-side $sort)
        groups.sort(key=lambda g: str(g.get('_id') or ''))

        all_chunks = []
        total_msg_count = 0

        for g in groups:
            msgs = g.get('messages') or []
            # Chronological within the session (was the server-side $sort on
            # messages.timestamp, dropped to stay under the 32MB sort limit)
            msgs.sort(key=lambda m: str(m.get('timestamp') or ''))

            session_chunks = []
            for msg in msgs:
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
                    prefix = 'User' if msg.get('role') == 'user' else 'Claude'
                    session_chunks.append(f'[{prefix}]: {text[:MAX_MSG_CHARS]}')
                    total_msg_count += 1

            if session_chunks:
                header = f'\n--- Session: {g.get("project", "?")} [{g.get("device", "?")}] ---\n'
                all_chunks.append(header)
                all_chunks.extend(session_chunks)

        output = '\n'.join(all_chunks)
        if len(output) > MAX_CHARS:
            output = output[:MAX_CHARS]

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
