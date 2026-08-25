#!/usr/bin/env python3
"""sessions_archive.py — cold-store old sessions as lzma blobs INSIDE MongoDB.

WHY A SECOND COLLECTION AND NOT A FLAG
    An archived document has a different SHAPE: one Binary blob instead of a
    `messages` array. Every existing reader assumes the array exists --
    mongo_recent.py unwinds `$messages`, warehouse_prune walks it, quiz_data
    reads it. A flag on `sessions` would make all of them silently skip or
    crash on the odd shape, and "silently" is the dangerous half. A separate
    collection means none of those scripts change at all.

    Same DATABASE, though: the M0 512 MB quota is cluster-wide, so a separate
    database would buy nothing.

WHAT IS COMPRESSED, AND WHAT DELIBERATELY IS NOT
    Only `messages` becomes a blob. Every piece of metadata -- session_id,
    project, device, counts, timestamps -- stays as plain queryable fields.
    That is the difference between an archive and a black box: you can still
    ask "what do I have from the MacBook in May" without restoring anything.
    You just cannot grep the conversation text until you restore it.

THE SAFETY ORDER IS NOT NEGOTIABLE
    compress -> write to sessions-archive -> READ IT BACK and compare against
    the original -> only then delete from sessions.

    Verification is a full equality check on the decompressed messages, not a
    length check. ~140 of the archivable sessions came from other machines and
    have no local .jsonl anywhere on this box, so for those the MongoDB copy is
    the ONLY copy. A corrupted blob that nobody verified would be silent,
    permanent data loss.

USAGE
    python sessions_archive.py --status
    python sessions_archive.py --archive --before 2026-06          # dry run
    python sessions_archive.py --archive --before 2026-06 --apply
    python sessions_archive.py --list
    python sessions_archive.py --restore <session-id>

    Dry run is the default. Nothing is written or deleted without --apply.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import lzma
import sys

from bson import json_util
from bson.binary import Binary

from _shared import force_utf8_io, get_db

force_utf8_io()

MB = 1024 * 1024
QUOTA_MB = 512
ARCHIVE_COL = 'sessions-archive'
PRESET = 6          # lzma preset; 9 buys ~2% for several times the CPU


def _month_key(doc) -> str:
    """Same convention as warehouse_prune.py:112 -- bucket by last_synced_at.

    NOTE this means "not written to Mongo since <month>", not "conversation
    started in <month>". That is the right axis for archiving: a session whose
    local transcript still exists gets re-synced when it changes, so a cold
    bucket really does mean nobody has touched it.
    """
    return str(doc.get('last_synced_at') or '')[:7]


def default_cutoff(months: int = 3) -> str:
    today = datetime.date.today().replace(day=1)
    for _ in range(months - 1):
        today = (today - datetime.timedelta(days=1)).replace(day=1)
    return today.strftime('%Y-%m')


def _pack(messages):
    """messages -> lzma blob. json_util keeps BSON types (ObjectId, dates)."""
    raw = json_util.dumps(messages).encode('utf-8')
    return raw, Binary(lzma.compress(raw, preset=PRESET))


def _unpack(blob):
    return json_util.loads(lzma.decompress(blob).decode('utf-8'))


# ---------- status ----------

def cmd_status(db, cutoff) -> int:
    col, arc = db['sessions'], db[ARCHIVE_COL]
    stats = db.command('dbStats', scale=1)
    used = stats['dataSize'] / MB
    print(f'  dataSize   {used:7.1f} MB   headroom {QUOTA_MB - used:6.1f} MB')
    print(f'  sessions   {col.count_documents({}):7,}')
    print(f'  archived   {arc.count_documents({}):7,}   (in {ARCHIVE_COL})')
    print(f'  cutoff     {cutoff}   -- months STRICTLY OLDER are eligible')
    print()

    by = collections.Counter()
    n = collections.Counter()
    for d in col.find({}, {'last_synced_at': 1, 'messages': 1, 'session_id': 1}):
        k = _month_key(d) or 'unknown'
        by[k] += len(json_util.dumps(d).encode('utf-8'))
        n[k] += 1

    print(f"  {'month':9} {'sessions':>8} {'size':>9} {'eligible':>9}")
    for k in sorted(by):
        mark = 'YES' if k < cutoff else '-'
        print(f'  {k:9} {n[k]:8} {by[k] / MB:8.1f}M {mark:>9}')

    elig = [k for k in by if k < cutoff]
    if not elig:
        print('\n  nothing older than the cutoff')
        return 0
    raw_mb = sum(by[k] for k in elig) / MB
    print(f'\n  eligible: {sum(n[k] for k in elig)} sessions, {raw_mb:.1f} MB raw')
    print('  run --archive to measure the real compression ratio (dry run)')
    return 0


# ---------- archive ----------

def cmd_archive(db, cutoff, apply: bool) -> int:
    col, arc = db['sessions'], db[ARCHIVE_COL]
    arc.create_index('session_id', unique=True)
    arc.create_index('month')

    docs = [d for d in col.find({}) if _month_key(d) and _month_key(d) < cutoff]
    if not docs:
        print(f'  nothing with last_synced_at older than {cutoff}')
        return 0

    print(f'  {len(docs)} session(s) eligible (last_synced_at < {cutoff})')
    print(f"  {'MODE: DRY RUN -- nothing will be written' if not apply else 'MODE: APPLY'}")
    print()

    tot_raw = tot_comp = 0
    ok = failed = 0
    for d in docs:
        sid = d['session_id']
        msgs = d.get('messages') or []
        raw, blob = _pack(msgs)
        doc_bytes = len(json_util.dumps(d).encode('utf-8'))
        tot_raw += doc_bytes
        tot_comp += len(blob)

        if not apply:
            ok += 1
            continue

        rec = {
            'session_id': sid,
            'month': _month_key(d),
            'device': d.get('device'),
            'project': d.get('project'),
            'session_name': d.get('session_name'),
            'message_count': d.get('message_count', len(msgs)),
            'raw_line_count': d.get('raw_line_count'),
            'session_started_at': d.get('session_started_at'),
            'session_started_at_kst': d.get('session_started_at_kst'),
            'last_synced_at': d.get('last_synced_at'),
            'last_synced_at_kst': d.get('last_synced_at_kst'),
            'truncated': d.get('truncated', False),
            'images_stripped': d.get('images_stripped'),
            'archived_at_kst': datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))).isoformat(),
            'codec': f'lzma:{PRESET}',
            'raw_bytes': len(raw),
            'blob': blob,
        }
        arc.replace_one({'session_id': sid}, rec, upsert=True)

        # READ BACK from the database -- not from the local variable. The point
        # is to prove what LANDED is intact, so a truncated write or a codec
        # mismatch is caught here and not months from now.
        back = arc.find_one({'session_id': sid}, {'blob': 1})
        if not back or _unpack(back['blob']) != msgs:
            print(f'  VERIFY FAILED {sid[:8]} -- left in sessions, NOT deleted')
            arc.delete_one({'session_id': sid})
            failed += 1
            continue

        col.delete_one({'session_id': sid})
        ok += 1

    ratio = tot_raw / max(tot_comp, 1)
    print(f'  raw        {tot_raw / MB:8.1f} MB')
    print(f'  compressed {tot_comp / MB:8.1f} MB   ({ratio:.1f}x)')
    print(f'  FREED      {(tot_raw - tot_comp) / MB:8.1f} MB')
    print()
    print(f'  archived {ok}, failed {failed}')
    if apply:
        s = db.command('dbStats', scale=1)
        u = s['dataSize'] / MB
        print(f'  dataSize now {u:.1f} MB, headroom {QUOTA_MB - u:.1f} MB')
    else:
        print('  (dry run -- re-run with --apply to perform it)')
    return 1 if failed else 0


# ---------- list / restore ----------

def cmd_list(db) -> int:
    arc = db[ARCHIVE_COL]
    rows = list(arc.find({}, {'blob': 0}).sort('last_synced_at_kst', 1))
    if not rows:
        print(f'  {ARCHIVE_COL} is empty')
        return 0
    print(f"  {'session':10} {'month':8} {'msgs':>6} {'raw':>9} {'device':22} project")
    for r in rows:
        print(f"  {r['session_id'][:8]:10} {r.get('month',''):8} "
              f"{r.get('message_count',0):6} {(r.get('raw_bytes') or 0)/MB:8.1f}M "
              f"{str(r.get('device'))[:22]:22} {str(r.get('project'))[-40:]}")
    print(f'\n  {len(rows)} archived session(s)')
    return 0


def cmd_restore(db, sid: str, apply: bool) -> int:
    arc, col = db[ARCHIVE_COL], db['sessions']
    r = arc.find_one({'session_id': {'$regex': '^' + sid}})
    if not r:
        print(f'  no archived session matching {sid}')
        return 1
    msgs = _unpack(r['blob'])
    print(f"  {r['session_id']}  {r.get('message_count')} msgs, "
          f"{(r.get('raw_bytes') or 0)/MB:.1f} MB raw")
    if not apply:
        print('  (dry run -- re-run with --apply to write it back to sessions)')
        return 0
    doc = {k: v for k, v in r.items()
           if k not in ('_id', 'blob', 'codec', 'raw_bytes', 'archived_at_kst', 'month')}
    doc['messages'] = msgs
    col.replace_one({'session_id': r['session_id']}, doc, upsert=True)
    arc.delete_one({'session_id': r['session_id']})
    print('  restored to sessions, removed from archive')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--archive', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--restore', metavar='SESSION_ID')
    ap.add_argument('--before', metavar='YYYY-MM', default=None)
    ap.add_argument('--apply', action='store_true', help='actually write/delete')
    a = ap.parse_args()

    cutoff = a.before or default_cutoff()
    client, db = get_db()
    try:
        if a.list:
            return cmd_list(db)
        if a.restore:
            return cmd_restore(db, a.restore, a.apply)
        if a.archive:
            return cmd_archive(db, cutoff, a.apply)
        return cmd_status(db, cutoff)
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())
