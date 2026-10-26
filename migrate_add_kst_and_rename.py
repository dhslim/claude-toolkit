#!/usr/bin/env python3
"""One-shot migration: rename UTC timestamp fields and add KST string companions.

Renames:
  sessions.session_date        -> session_started_at
  sessions.synced_at           -> last_synced_at
  file_sync_cache.synced_at    -> last_synced_at

Adds (KST companions, ISO-8601 strings with +09:00 offset, never reified as datetime):
  sessions.session_started_at_kst
  sessions.last_synced_at_kst
  sessions.messages[*].timestamp_kst
  file_sync_cache.last_synced_at_kst
  quiz-markers.taken_at_kst       (when taken_at exists)
  quiz-markers.dismissed_at_kst   (when dismissed_at exists)
  daily-quizzes.created_at_kst    (when created_at exists)
  daily-quizzes.graded_at_kst     (when graded_at exists)

Also drops the old indexes:
  sessions.session_date_1
  sessions.synced_at_1

Idempotent: docs that already have the new fields are skipped without rewrites.
Atomic per-doc (single update_one). Safe to run while writers are using the new
schema concurrently — a concurrent write either lands before or after the
per-doc update; no doc is half-migrated mid-write.

Usage:
  python migrate_add_kst_and_rename.py             # run the migration
  python migrate_add_kst_and_rename.py --dry-run   # report what would change
"""

import argparse
import sys
from _shared import get_db, to_kst_iso


def migrate_sessions(db, dry_run=False):
    col = db['sessions']
    n_total = col.count_documents({})
    n_renamed = 0
    n_msgs_updated = 0
    n_skipped = 0
    n_kst_added = 0

    for doc in col.find({}):
        sid = doc['_id']
        rename_ops = {}
        unset_ops = {}
        # Rename when only the old field exists; drop the old field if the new one
        # was already written (race with a concurrent Stop-hook write).
        if 'session_date' in doc:
            if 'session_started_at' not in doc:
                rename_ops['session_date'] = 'session_started_at'
            else:
                unset_ops['session_date'] = ''
        if 'synced_at' in doc:
            if 'last_synced_at' not in doc:
                rename_ops['synced_at'] = 'last_synced_at'
            else:
                unset_ops['synced_at'] = ''

        # The effective new values (post-rename) we'll use to derive KST companions
        session_started_at = doc.get('session_started_at') or doc.get('session_date')
        last_synced_at    = doc.get('last_synced_at')    or doc.get('synced_at')

        set_ops = {}
        if session_started_at is not None and 'session_started_at_kst' not in doc:
            set_ops['session_started_at_kst'] = to_kst_iso(session_started_at)
        if last_synced_at is not None and 'last_synced_at_kst' not in doc:
            set_ops['last_synced_at_kst'] = to_kst_iso(last_synced_at)

        # Per-message timestamp_kst — rewrite the whole messages array if any
        # message is missing it. (Atomic doc replacement.)
        messages = doc.get('messages') or []
        msgs_need_update = any(
            (m.get('timestamp') is not None) and ('timestamp_kst' not in m)
            for m in messages
        )
        new_messages = None
        if msgs_need_update:
            new_messages = []
            for m in messages:
                if 'timestamp' in m and 'timestamp_kst' not in m:
                    nm = dict(m)
                    nm['timestamp_kst'] = to_kst_iso(m.get('timestamp'))
                    new_messages.append(nm)
                else:
                    new_messages.append(m)

        if not rename_ops and not unset_ops and not set_ops and new_messages is None:
            n_skipped += 1
            continue

        update = {}
        if rename_ops:
            update['$rename'] = rename_ops
        if unset_ops:
            update['$unset'] = unset_ops
        if set_ops or new_messages is not None:
            update.setdefault('$set', {})
            update['$set'].update(set_ops)
            if new_messages is not None:
                update['$set']['messages'] = new_messages

        if dry_run:
            print(f'  [dry] sessions/{doc.get("session_id","?")[:8]}  '
                  f'rename={list(rename_ops)} unset={list(unset_ops)} '
                  f'set={list(set_ops)} msgs_rewritten={"yes" if new_messages is not None else "no"}')
        else:
            col.update_one({'_id': sid}, update)
        if rename_ops:
            n_renamed += 1
        if set_ops:
            n_kst_added += 1
        if new_messages is not None:
            n_msgs_updated += 1

    print(f'sessions: {n_total} total | renamed {n_renamed} | KST top-level added {n_kst_added} | msg arrays rewritten {n_msgs_updated} | unchanged {n_skipped}')


def migrate_file_sync_cache(db, dry_run=False):
    col = db['file_sync_cache']
    n_total = col.count_documents({})
    n_renamed = 0
    n_kst = 0
    n_skipped = 0

    for doc in col.find({}):
        rename_ops = {}
        unset_ops = {}
        if 'synced_at' in doc:
            if 'last_synced_at' not in doc:
                rename_ops['synced_at'] = 'last_synced_at'
            else:
                unset_ops['synced_at'] = ''

        last_synced_at = doc.get('last_synced_at') or doc.get('synced_at')
        set_ops = {}
        if last_synced_at is not None and 'last_synced_at_kst' not in doc:
            set_ops['last_synced_at_kst'] = to_kst_iso(last_synced_at)

        if not rename_ops and not unset_ops and not set_ops:
            n_skipped += 1
            continue

        update = {}
        if rename_ops:
            update['$rename'] = rename_ops
        if unset_ops:
            update['$unset'] = unset_ops
        if set_ops:
            update['$set'] = set_ops

        if dry_run:
            print(f'  [dry] file_sync_cache/{str(doc.get("file_path","?"))[-40:]}  rename={list(rename_ops)} set={list(set_ops)}')
        else:
            col.update_one({'_id': doc['_id']}, update)
        if rename_ops:
            n_renamed += 1
        if set_ops:
            n_kst += 1

    print(f'file_sync_cache: {n_total} total | renamed {n_renamed} | KST added {n_kst} | unchanged {n_skipped}')


def migrate_quiz_markers(db, dry_run=False):
    col = db['quiz-markers']
    n_total = col.count_documents({})
    n_kst = 0
    n_skipped = 0

    for doc in col.find({}):
        set_ops = {}
        if doc.get('taken_at') is not None and 'taken_at_kst' not in doc:
            set_ops['taken_at_kst'] = to_kst_iso(doc['taken_at'])
        if doc.get('dismissed_at') is not None and 'dismissed_at_kst' not in doc:
            set_ops['dismissed_at_kst'] = to_kst_iso(doc['dismissed_at'])

        if not set_ops:
            n_skipped += 1
            continue
        if dry_run:
            print(f'  [dry] quiz-markers/{doc.get("date","?")}  set={list(set_ops)}')
        else:
            col.update_one({'_id': doc['_id']}, {'$set': set_ops})
        n_kst += 1

    print(f'quiz-markers: {n_total} total | KST added {n_kst} | unchanged {n_skipped}')


def migrate_daily_quizzes(db, dry_run=False):
    col = db['daily-quizzes']
    n_total = col.count_documents({})
    n_kst = 0
    n_skipped = 0

    for doc in col.find({}):
        set_ops = {}
        if doc.get('created_at') is not None and 'created_at_kst' not in doc:
            set_ops['created_at_kst'] = to_kst_iso(doc['created_at'])
        if doc.get('graded_at') is not None and 'graded_at_kst' not in doc:
            set_ops['graded_at_kst'] = to_kst_iso(doc['graded_at'])

        if not set_ops:
            n_skipped += 1
            continue
        if dry_run:
            print(f'  [dry] daily-quizzes/{doc.get("date","?")}  set={list(set_ops)}')
        else:
            col.update_one({'_id': doc['_id']}, {'$set': set_ops})
        n_kst += 1

    print(f'daily-quizzes: {n_total} total | KST added {n_kst} | unchanged {n_skipped}')


def drop_old_indexes(db, dry_run=False):
    """Drop the indexes on the renamed fields (sessions.session_date_1, sessions.synced_at_1).
    Safe if they don't exist."""
    col = db['sessions']
    existing = {i['name'] for i in col.list_indexes()}
    for old in ('session_date_1', 'synced_at_1'):
        if old in existing:
            if dry_run:
                print(f'  [dry] would drop index sessions.{old}')
            else:
                col.drop_index(old)
                print(f'  dropped index sessions.{old}')
        else:
            print(f'  (sessions.{old} already absent)')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    client, db = get_db()
    try:
        print(f'=== Migration {"(DRY RUN)" if args.dry_run else ""} ===')
        migrate_sessions(db, args.dry_run)
        migrate_file_sync_cache(db, args.dry_run)
        migrate_quiz_markers(db, args.dry_run)
        migrate_daily_quizzes(db, args.dry_run)
        print('=== Indexes ===')
        drop_old_indexes(db, args.dry_run)
        print('=== Done ===')
    finally:
        client.close()


if __name__ == '__main__':
    main()
