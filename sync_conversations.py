#!/usr/bin/env python3
"""Sync Claude Code conversation transcripts to MongoDB Atlas.

Modes:
  --scan       Full scan of all JSONL files (cron mode)
  --file PATH  Sync a single file
  (default)    Read stdin for hook JSON, fallback to --scan
"""

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from _shared import get_db, with_retry

CLAUDE_PROJECTS_DIR = Path.home() / '.claude' / 'projects'
DEVICE = platform.node()

# Message types to skip
SKIP_TYPES = frozenset([
    'file-history-snapshot', 'progress', 'last-prompt', 'queue-operation'
])


def parse_jsonl(file_path):
    """Parse a JSONL transcript file, returning filtered session data."""
    text = Path(file_path).read_text(encoding='utf-8')
    lines = [l for l in text.strip().split('\n') if l]
    raw_line_count = len(lines)

    session_id = None
    session_name = None
    session_date = None
    project = None
    messages = []

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get('type')

        if not session_id and obj.get('sessionId'):
            session_id = obj['sessionId']
        if msg_type == 'custom-title' and obj.get('customTitle'):
            session_name = obj['customTitle']
        if not session_date and obj.get('timestamp'):
            session_date = datetime.fromisoformat(obj['timestamp'].replace('Z', '+00:00'))
        if not project and obj.get('cwd'):
            project = obj['cwd']

        if msg_type in SKIP_TYPES:
            continue

        msg = obj.get('message')

        # Filter local-command-caveat system wrappers
        if (msg_type == 'user' and isinstance(msg, dict)
                and isinstance(msg.get('content'), str)
                and '<local-command-caveat>' in msg['content']):
            continue

        messages.append({
            'type': msg_type,
            'role': msg.get('role') if isinstance(msg, dict) else None,
            'content': msg.get('content') if isinstance(msg, dict) else None,
            'timestamp': obj.get('timestamp'),
            'uuid': obj.get('uuid'),
        })

    if not session_id:
        session_id = Path(file_path).stem
    if not project:
        project = Path(file_path).parent.name

    return {
        'session_id': session_id,
        'session_name': session_name,
        'project': project,
        'session_date': session_date,
        'raw_line_count': raw_line_count,
        'messages': messages,
    }


def count_lines(file_path):
    """Fast line count for a JSONL file."""
    text = Path(file_path).read_text(encoding='utf-8')
    return len([l for l in text.strip().split('\n') if l])


def find_all_jsonl_files():
    """Recursively find all .jsonl files under CLAUDE_PROJECTS_DIR."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    return list(CLAUDE_PROJECTS_DIR.rglob('*.jsonl'))


def upsert_session(collection, doc):
    """Upsert a session document with retry."""
    def _do():
        result = collection.update_one(
            {'session_id': doc['session_id']},
            {'$set': {
                'session_name': doc['session_name'],
                'project': doc['project'],
                'device': doc['device'],
                'session_date': doc['session_date'],
                'synced_at': datetime.now(timezone.utc),
                'message_count': doc['message_count'],
                'raw_line_count': doc['raw_line_count'],
                'messages': doc['messages'],
            }},
            upsert=True,
        )
        if result.upserted_id:
            return 'inserted'
        return 'updated' if result.modified_count > 0 else 'unchanged'
    return with_retry(_do)


def sync_one_file(collection, file_path):
    """Sync a single JSONL file to MongoDB."""
    parsed = parse_jsonl(file_path)
    if not parsed['session_id'] or not parsed['messages']:
        return None

    doc = {
        'session_id': parsed['session_id'],
        'session_name': parsed['session_name'],
        'project': parsed['project'],
        'device': DEVICE,
        'session_date': parsed['session_date'] or datetime.now(timezone.utc),
        'message_count': len(parsed['messages']),
        'raw_line_count': parsed['raw_line_count'],
        'messages': parsed['messages'],
    }

    # BSON 16MB limit check (~14MB safety margin)
    estimated_size = len(json.dumps(doc, default=str).encode('utf-8'))
    if estimated_size > 14 * 1024 * 1024:
        print(f'Warning: session {parsed["session_id"]} too large '
              f'({estimated_size / 1024 / 1024:.1f}MB) — truncating messages',
              file=sys.stderr)
        while (len(json.dumps(doc, default=str).encode('utf-8')) > 14 * 1024 * 1024
               and len(doc['messages']) > 10):
            doc['messages'] = doc['messages'][:int(len(doc['messages']) * 0.8)]
            doc['message_count'] = len(doc['messages'])

    action = upsert_session(collection, doc)
    return {
        'session_id': parsed['session_id'],
        'action': action,
        'message_count': len(doc['messages']),
    }


def sync_all(collection):
    """Full scan: sync all JSONL files, using line-count cache to skip unchanged."""
    db = collection.database
    cache_col = db['file_sync_cache']
    cache_col.create_index('file_path', unique=True)

    files = find_all_jsonl_files()
    print(f'Found {len(files)} JSONL files')

    # Build cache map
    existing = list(cache_col.find({}))
    cache_map = {e['file_path']: e['line_count'] for e in existing}

    inserted = updated = skipped = errors = 0

    for fp in files:
        try:
            normalized = str(fp).replace('\\', '/')
            current_lines = count_lines(fp)

            if cache_map.get(normalized) == current_lines:
                skipped += 1
                continue

            result = sync_one_file(collection, fp)
            if result:
                if result['action'] == 'inserted':
                    inserted += 1
                elif result['action'] == 'updated':
                    updated += 1
                else:
                    skipped += 1

                # Update cache
                with_retry(lambda n=normalized, cl=current_lines: cache_col.update_one(
                    {'file_path': n},
                    {'$set': {'file_path': n, 'line_count': cl,
                              'synced_at': datetime.now(timezone.utc)}},
                    upsert=True,
                ))
        except Exception as e:
            errors += 1
            print(f'Error {fp.name}: {e}', file=sys.stderr)

    print(f'Done: {inserted} inserted, {updated} updated, {skipped} skipped, {errors} errors')


def sync_from_hook(collection, stdin_data):
    """Hook mode: parse stdin JSON for transcript_path and sync that file."""
    try:
        hook_data = json.loads(stdin_data)
    except json.JSONDecodeError:
        print('stdin JSON parse failed, falling back to scan', file=sys.stderr)
        return sync_all(collection)

    transcript_path = hook_data.get('transcript_path') or hook_data.get('session_id')
    if not transcript_path:
        print('No transcript_path, running full scan')
        return sync_all(collection)

    file_path = Path(transcript_path)
    if not file_path.exists():
        files = find_all_jsonl_files()
        match = next((f for f in files if f.stem == transcript_path), None)
        if not match:
            print(f'File not found: {transcript_path}', file=sys.stderr)
            return
        file_path = match

    result = sync_one_file(collection, file_path)
    if result:
        print(f'{result["action"]}: {result["session_id"]} ({result["message_count"]} messages)')


def read_stdin(timeout_ms=500):
    """Try to read stdin with a timeout. Returns data or empty string."""
    import select
    if sys.platform == 'win32':
        # Windows: can't select on stdin; try non-blocking read
        import msvcrt
        import time
        data = ''
        deadline = time.monotonic() + timeout_ms / 1000
        # If stdin is a pipe/file, read it all
        if not sys.stdin.isatty():
            return sys.stdin.read().strip()
        # If it's a TTY, nothing piped
        return ''
    else:
        ready, _, _ = select.select([sys.stdin], [], [], timeout_ms / 1000)
        if ready:
            return sys.stdin.read().strip()
        return ''


def main():
    parser = argparse.ArgumentParser(description='Sync Claude conversations to MongoDB')
    parser.add_argument('--scan', action='store_true', help='Full scan mode')
    parser.add_argument('--file', dest='file_path', help='Sync a single file')
    args = parser.parse_args()

    client, db = get_db()
    try:
        collection = db['sessions']
        collection.create_index('session_id', unique=True)
        collection.create_index('session_date')

        if args.file_path:
            resolved = Path(args.file_path).resolve()
            if not resolved.exists():
                print(f'File not found: {resolved}', file=sys.stderr)
                sys.exit(1)
            result = sync_one_file(collection, resolved)
            if result:
                print(f'{result["action"]}: {result["session_id"]} ({result["message_count"]} messages)')
        elif args.scan:
            sync_all(collection)
        else:
            stdin_data = read_stdin()
            if stdin_data:
                sync_from_hook(collection, stdin_data)
            else:
                sync_all(collection)
    except Exception as e:
        print(f'Sync failed: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
