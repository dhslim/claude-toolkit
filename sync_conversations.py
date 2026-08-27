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
import subprocess
import sys

# Windows: these hooks run under pythonw.exe, which has NO console. A console
# child (git.exe) spawned from a console-less parent must CREATE its own console
# — a window that flashes on screen at every turn end. CREATE_NO_WINDOW suppresses
# it. Zero elsewhere (the flag is Windows-only).
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
from datetime import datetime, timezone
from pathlib import Path

from _shared import force_utf8_io, get_db, to_kst_iso, with_retry

CLAUDE_PROJECTS_DIR = Path.home() / '.claude' / 'projects'
DEVICE = platform.node()

# Message types to skip
SKIP_TYPES = frozenset([
    'file-history-snapshot', 'progress', 'last-prompt', 'queue-operation'
])

TOOLKIT_REPO = Path(__file__).resolve().parent

# Version provenance: the claude-toolkit commit this sync is running under, so
# every session doc records which toolkit version produced it. Computed once per
# process (identical for every file in a scan); fails soft to None outside git.
_TOOLKIT_INFO = None


def toolkit_commit_info():
    """Return {'toolkit_commit', 'toolkit_commit_date', 'toolkit_dirty'}.

    toolkit_commit = short SHA, toolkit_commit_date = committer ISO-8601 date,
    toolkit_dirty = uncommitted changes present. Any field is None / False if git
    is unavailable. Cached for the process (same for all files in one scan).
    """
    global _TOOLKIT_INFO
    if _TOOLKIT_INFO is not None:
        return _TOOLKIT_INFO

    def _git(*args):
        return subprocess.run(
            ['git', '-C', str(TOOLKIT_REPO), *args],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW,
        ).stdout.strip()

    info = {'toolkit_commit': None, 'toolkit_commit_date': None, 'toolkit_dirty': False}
    try:
        info['toolkit_commit'] = _git('rev-parse', '--short', 'HEAD') or None
        info['toolkit_commit_date'] = _git('log', '-1', '--format=%cI') or None
        info['toolkit_dirty'] = bool(_git('status', '--porcelain'))
    except Exception:
        pass
    _TOOLKIT_INFO = info
    return info


def drop_thinking_signatures(content):
    """Strip `signature` from extended-thinking blocks on the way into Mongo.

    An extended-thinking block arrives as {type, thinking, signature}, where the
    signature is a cryptographic seal proving the reasoning was not tampered
    with when the conversation is replayed to the API. Claude Code does not
    persist the reasoning TEXT to the transcript -- measured over 1,866 blocks,
    `thinking` was empty in every single one -- so what reaches the warehouse is
    a seal over nothing. Individual seals run from 380 to 12,000+ characters.

    It is also the worst possible thing to store: cryptographic output is
    maximum-entropy by construction (a compressible signature would be a broken
    one), so it never compresses. 187 MB across the warehouse, 37% of the whole
    database, and the reason every codec converged at ~2.5x.

    The BLOCK is kept, only the field is dropped: removing the block would change
    message shapes and block counts that readers may depend on.

    This only affects the copy written to MongoDB. ~/.claude/projects/**.jsonl is
    never modified -- `claude --resume` replays those to the API and the API
    checks the signatures, so stripping them there could break resumption.
    """
    if not isinstance(content, list):
        return content
    for block in content:
        if (isinstance(block, dict) and block.get('type') == 'thinking'
                and 'signature' in block):
            del block['signature']
    return content


def parse_jsonl(file_path):
    """Parse a JSONL transcript file, returning filtered session data."""
    text = Path(file_path).read_text(encoding='utf-8')
    lines = [l for l in text.strip().split('\n') if l]
    raw_line_count = len(lines)

    session_id = None
    session_name = None
    session_started_at = None
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
        if not session_started_at and obj.get('timestamp'):
            session_started_at = datetime.fromisoformat(obj['timestamp'].replace('Z', '+00:00'))
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

        ts = obj.get('timestamp')
        messages.append({
            'type': msg_type,
            'role': msg.get('role') if isinstance(msg, dict) else None,
            'content': drop_thinking_signatures(
                msg.get('content') if isinstance(msg, dict) else None),
            'timestamp': ts,
            'timestamp_kst': to_kst_iso(ts),
            'uuid': obj.get('uuid'),
            'parentUuid': obj.get('parentUuid'),
        })

    if not session_id:
        session_id = Path(file_path).stem
    if not project:
        project = Path(file_path).parent.name

    return {
        'session_id': session_id,
        'session_name': session_name,
        'project': project,
        'session_started_at': session_started_at,
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
        now_utc = datetime.now(timezone.utc)
        result = collection.update_one(
            {'session_id': doc['session_id']},
            {'$set': {
                'session_name': doc['session_name'],
                'project': doc['project'],
                'device': doc['device'],
                'session_started_at': doc['session_started_at'],
                'session_started_at_kst': to_kst_iso(doc['session_started_at']),
                'last_synced_at': now_utc,
                'last_synced_at_kst': to_kst_iso(now_utc),
                'message_count': doc['message_count'],
                'raw_line_count': doc['raw_line_count'],
                'messages': doc['messages'],
                'truncated': doc.get('truncated', False),
                'truncated_dropped_oldest': doc.get('truncated_dropped_oldest', 0),
                'toolkit_commit': doc.get('toolkit_commit'),
                'toolkit_commit_date': doc.get('toolkit_commit_date'),
                'toolkit_dirty': doc.get('toolkit_dirty', False),
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
        'session_started_at': parsed['session_started_at'] or datetime.now(timezone.utc),
        'message_count': len(parsed['messages']),
        'raw_line_count': parsed['raw_line_count'],
        'messages': parsed['messages'],
        **toolkit_commit_info(),
    }

    # BSON 16MB limit check (~14MB safety margin). Long-lived (e.g. proxy-trimmed)
    # sessions can exceed this. Keep the NEWEST messages and drop the oldest — the
    # warehouse is for recent-activity recall (mgo, quiz, reports), so the recent
    # tail is what matters. NOTE: was [:0.8] (keep-oldest), which silently dropped
    # the most recent work from any >14MB session; now [-0.8:] (keep-newest).
    estimated_size = len(json.dumps(doc, default=str, ensure_ascii=False).encode('utf-8'))
    doc['truncated'] = False
    doc['truncated_dropped_oldest'] = 0
    if estimated_size > 14 * 1024 * 1024:
        before = len(doc['messages'])
        while (len(json.dumps(doc, default=str, ensure_ascii=False).encode('utf-8')) > 14 * 1024 * 1024
               and len(doc['messages']) > 10):
            doc['messages'] = doc['messages'][-int(len(doc['messages']) * 0.8):]  # keep newest 80%
            doc['message_count'] = len(doc['messages'])
        doc['truncated'] = True
        doc['truncated_dropped_oldest'] = before - len(doc['messages'])
        # Loud: goes to stderr AND is persisted on the doc (query {truncated:true}).
        print(f'TRUNCATED {parsed["session_id"]}: kept newest {len(doc["messages"])} of '
              f'{before} msgs ({doc["truncated_dropped_oldest"]} oldest dropped) — '
              f'source was {estimated_size / 1024 / 1024:.1f}MB', file=sys.stderr)

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
                def _update_cache(n=normalized, cl=current_lines):
                    now_utc = datetime.now(timezone.utc)
                    return cache_col.update_one(
                        {'file_path': n},
                        {'$set': {'file_path': n, 'line_count': cl,
                                  'last_synced_at': now_utc,
                                  'last_synced_at_kst': to_kst_iso(now_utc)}},
                        upsert=True,
                    )
                with_retry(_update_cache)
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
    if sys.platform == 'win32':
        # Windows: select() doesn't work on stdin; check if piped
        if not sys.stdin.isatty():
            return sys.stdin.read().strip()
        return ''
    else:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout_ms / 1000)
        if ready:
            return sys.stdin.read().strip()
        return ''


def main():
    force_utf8_io()
    parser = argparse.ArgumentParser(description='Sync Claude conversations to MongoDB')
    parser.add_argument('--scan', action='store_true', help='Full scan mode')
    parser.add_argument('--file', dest='file_path', help='Sync a single file')
    args = parser.parse_args()

    client, db = get_db()
    try:
        collection = db['sessions']
        collection.create_index('session_id', unique=True)
        collection.create_index('session_started_at')

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
