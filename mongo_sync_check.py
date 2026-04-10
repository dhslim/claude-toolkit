#!/usr/bin/env python3
"""Compare local JSONL transcripts against what's synced in MongoDB.

Flags sessions where the local file has more lines than MongoDB recorded,
which indicates the sync hook may have failed.

Usage: mongo_sync_check.py [session_id_prefix]
  No args    → check all sessions synced in the last 24h
  abc123     → check only sessions matching that prefix
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _shared import get_db

CLAUDE_PROJECTS_DIR = Path.home() / '.claude' / 'projects'


def find_jsonl(session_id):
    """Find the JSONL file for a given session ID."""
    for f in CLAUDE_PROJECTS_DIR.rglob('*.jsonl'):
        if session_id in f.stem:
            return f
    return None


def count_lines(path):
    text = path.read_text(encoding='utf-8')
    return len([l for l in text.strip().split('\n') if l])


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else None

    client, db = get_db()
    col = db['sessions']

    if prefix:
        query = {'session_id': {'$regex': f'^{prefix}'}}
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        query = {'synced_at': {'$gte': cutoff}}

    sessions = list(col.find(query).sort('synced_at', -1))
    if not sessions:
        print('No sessions found.')
        client.close()
        return

    issues = []
    ok = []

    for s in sessions:
        sid = s['session_id']
        mongo_lines = s.get('raw_line_count', 0)
        mongo_msgs = s.get('message_count', 0)
        synced_at = s.get('synced_at')
        project = s.get('project', '?')

        local_file = find_jsonl(sid)
        if not local_file:
            continue

        local_lines = count_lines(local_file)
        diff = local_lines - mongo_lines

        entry = {
            'session_id': sid[:12],
            'project': Path(project).name,
            'local_lines': local_lines,
            'mongo_lines': mongo_lines,
            'mongo_msgs': mongo_msgs,
            'diff': diff,
            'synced_at': str(synced_at),
        }

        if diff > 0:
            issues.append(entry)
        else:
            ok.append(entry)

    if issues:
        print(f'⚠ {len(issues)} session(s) with unsynced lines:\n')
        for e in issues:
            print(f'  {e["session_id"]}  {e["project"]}')
            print(f'    local: {e["local_lines"]} lines  |  mongo: {e["mongo_lines"]} lines  |  +{e["diff"]} unsynced')
            print(f'    mongo msgs: {e["mongo_msgs"]}  |  last sync: {e["synced_at"]}')
            print()
    else:
        print('All sessions in sync.')

    if ok:
        print(f'{len(ok)} session(s) OK:')
        for e in ok:
            print(f'  {e["session_id"]}  {e["project"]:30s}  {e["local_lines"]} lines  ✓')

    client.close()


if __name__ == '__main__':
    main()
