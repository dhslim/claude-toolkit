#!/usr/bin/env python3
"""warehouse_prune.py — keep the MongoDB warehouse inside its quota.

THE MODEL: HOT TIER / COLD TIER
    MongoDB is the HOT tier — recent months, instantly queryable by /mgo.
    ~/.claude/warehouse-archive/ is the COLD tier — full-fidelity JSON on disk,
    including the image bytes the hot tier drops. Nothing is ever destroyed; it
    moves down a tier. Every operation here is reversible via --restore.

    Two levers, cheapest first:
      --strip     replace image payloads with tombstones, KEEP all text (in place)
      --archive   move whole sessions to disk and delete them from MongoDB

    --strip is almost always enough: images are ~20% of the warehouse overall but
    34-40% of months 3+ months old, because screenshots age worse than text.

WHAT ATLAS ACTUALLY MEASURES (this cost a morning to learn)
    The M0 free tier's 512 MB quota tracks `dataSize`, NOT `storageSize`.
    That distinction decides whether this tool works at all:

      dataSize     the logical size of documents      -> what the quota counts
      storageSize  bytes WiredTiger has on disk       -> does NOT shrink on delete

    So `db.stats()` will keep reporting the same storageSize after a large prune
    (WiredTiger reuses freed blocks internally rather than returning them to the
    OS, and M0 does not permit `compact`). That is expected and harmless. Watch
    dataSize; it drops immediately, and the quota lifts with it.

WHEN THE CLUSTER IS ALREADY OVER QUOTA, ONLY DELETES WORK
    Verified against a blocked cluster:

      insert_one   BLOCKED   "you are over your space quota"
      update_one   BLOCKED   <- a --strip is an update, so it CANNOT run
      delete_one   ALLOWED   <- the only escape hatch

    This is a chicken-and-egg: the cheap, non-destructive fix (--strip) needs
    write permission that only whole-document deletion can buy back. If you are
    already blocked, run `--archive` on the oldest month FIRST to get under the
    line, then `--strip` everything else. `--status` tells you which situation
    you are in rather than making you guess.

THE TOMBSTONE
    A stripped image leaves ~100 bytes in place of ~350 KB:

      {"type": "image_stripped", "bytes": 86302, "sha256": "10d1569454d9cc89",
       "media_type": "image/png", "stripped_at": "2026-08-13"}

    It is deliberately NOT a silent deletion. /mgo on an old session reports
    "an 86 KB PNG was here" instead of showing a gap, and the sha256 matches the
    file in ~/.claude/image-cache/ if it still exists locally. An absent block
    and a stripped block must never look the same to a reader.

ARCHIVE BEFORE YOU MODIFY, ALWAYS
    Every document is written to the cold tier and read back (session_id and
    message count compared) BEFORE MongoDB is touched. A document whose archive
    fails verification is skipped, not modified. This is why the operations here
    are safe to run against history that exists nowhere else — and much of it
    does: sessions arrive from several machines, and the local .jsonl for older
    ones is usually long gone.

WATCH FOR WORKTREE TRIPLICATION
    A session worked on via git worktrees can be stored two or three times under
    different project paths, with the same session_started_at and the same image
    hashes but slightly different message counts. Three copies of one STT session
    were what pushed this cluster over its quota. `--status` flags suspected
    duplicate groups; they are the cheapest thing to archive because the copies
    are near-identical.

USAGE
    python warehouse_prune.py --status                      # am I full, and why?
    python warehouse_prune.py --strip                       # dry run (default)
    python warehouse_prune.py --strip --apply
    python warehouse_prune.py --strip --before 2026-06 --apply
    python warehouse_prune.py --archive --before 2026-05 --apply
    python warehouse_prune.py --list-archive
    python warehouse_prune.py --restore <session-id>

    Dry run is the default for --strip and --archive. Nothing writes without
    --apply.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import json
import sys
from pathlib import Path

from bson import json_util

from _shared import get_db

MB = 1024 * 1024
ARCHIVE_ROOT = Path.home() / '.claude' / 'warehouse-archive'
STRIPPED_DIR = ARCHIVE_ROOT / 'stripped'      # docs that were stripped in place
ARCHIVED_DIR = ARCHIVE_ROOT / 'archived'      # docs removed from MongoDB entirely
QUOTA_MB = 512                                # Atlas M0

# Keep this many months hot by default. At ~120 MB/month un-stripped (~95 MB
# stripped), three months is the most that fits a 512 MB tier with headroom.
DEFAULT_HOT_MONTHS = 3


def _blob(v) -> int:
    return len(json.dumps(v, default=str).encode('utf-8'))


def _month_key(doc) -> str:
    return str(doc.get('last_synced_at') or '')[:7]


def default_cutoff(months: int = DEFAULT_HOT_MONTHS) -> str:
    """First month to KEEP hot. Anything strictly older is eligible."""
    today = datetime.date.today().replace(day=1)
    for _ in range(months - 1):
        today = (today - datetime.timedelta(days=1)).replace(day=1)
    return today.strftime('%Y-%m')


# ---------- status ----------

def cmd_status(db) -> int:
    col = db['sessions']
    s = db.command('dbStats', scale=1)
    data_mb = s['dataSize'] / MB
    print(f"  dataSize    {data_mb:8.1f} MB   <- what the {QUOTA_MB} MB quota counts")
    print(f"  storageSize {s['storageSize'] / MB:8.1f} MB   <- does not shrink on delete; ignore it")
    print(f"  headroom    {QUOTA_MB - data_mb:8.1f} MB")
    print(f"  sessions    {col.count_documents({}):8,}")

    # The only question that matters operationally.
    probe = {'session_id': '__warehouse_prune_probe__'}
    verdicts = {}
    for name, fn in (('update', lambda: col.update_one(probe, {'$unset': {'x': ''}})),
                     ('delete', lambda: col.delete_one(probe))):
        try:
            fn()
            verdicts[name] = 'ALLOWED'
        except Exception as e:
            verdicts[name] = f'BLOCKED ({type(e).__name__})'
    print(f"\n  writes: update={verdicts['update']}  delete={verdicts['delete']}")
    if verdicts['update'].startswith('BLOCKED'):
        print("  -> over quota: --strip cannot run. Use --archive on the oldest")
        print("     month first to get under the line, then --strip.")

    by_month = collections.Counter()
    img_month = collections.Counter()
    n_month = collections.Counter()
    started = collections.defaultdict(list)
    for d in col.find({}):
        k = _month_key(d) or 'unknown'
        by_month[k] += len(json_util.dumps(d).encode('utf-8'))
        n_month[k] += 1
        img_month[k] += _image_bytes(d.get('messages'))[0]
        st = d.get('session_started_at')
        if st:
            started[str(st)].append(d.get('session_id', '?')[:8])

    print(f"\n  {'month':9} {'sessions':>8} {'size':>9} {'images':>9} {'share':>7}")
    for k in sorted(by_month, reverse=True):
        print(f"  {k:9} {n_month[k]:8} {by_month[k] / MB:8.1f}M {img_month[k] / MB:8.1f}M "
              f"{img_month[k] / max(by_month[k], 1) * 100:6.1f}%")

    dupes = {k: v for k, v in started.items() if len(v) > 1}
    if dupes:
        print(f"\n  suspected worktree duplicates ({len(dupes)} group(s)) — same start time:")
        for st, sids in list(dupes.items())[:5]:
            print(f"    {st[:19]}  {', '.join(sids)}")
        print("    these are the cheapest thing to --archive: near-identical copies")
    return 0


def _image_bytes(msgs):
    total = count = 0
    for m in msgs or []:
        c = m.get('content')
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get('type') == 'image':
                    total += _blob(b)
                    count += 1
    return total, count


# ---------- strip ----------

def _tombstone(block, today) -> dict:
    src = block.get('source') or {}
    return {
        'type': 'image_stripped',
        'bytes': _blob(block),
        'sha256': hashlib.sha256(str(src.get('data') or '').encode('utf-8')).hexdigest()[:16],
        'media_type': src.get('media_type'),
        'stripped_at': today,
    }


def _strip_messages(msgs, today):
    n = freed = 0
    out = []
    for m in msgs or []:
        c = m.get('content')
        if isinstance(c, list):
            new_c = []
            for b in c:
                if isinstance(b, dict) and b.get('type') == 'image':
                    t = _tombstone(b, today)
                    freed += _blob(b) - _blob(t)
                    n += 1
                    new_c.append(t)
                else:
                    new_c.append(b)
            m = {**m, 'content': new_c}
        out.append(m)
    return out, n, freed


def _archive_doc(doc, dest_dir) -> Path | None:
    """Write to the cold tier and read it back. Returns None if unverifiable."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = dest_dir / f"session-{doc['session_id']}.json"
    p.write_text(json_util.dumps(doc), encoding='utf-8')
    try:
        rt = json_util.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None
    if rt.get('session_id') != doc.get('session_id'):
        return None
    if len(rt.get('messages') or []) != len(doc.get('messages') or []):
        return None
    return p


def cmd_strip(db, cutoff, apply_) -> int:
    col = db['sessions']
    today = datetime.date.today().isoformat()
    print(f"  {'APPLY' if apply_ else 'DRY RUN'} — tombstone images synced before {cutoff}\n")
    docs = imgs = freed = 0
    for d in col.find({}):
        k = _month_key(d)
        if not k or k >= cutoff:
            continue
        new_msgs, n, f = _strip_messages(d.get('messages'), today)
        if not n:
            continue
        print(f"    {d['session_id'][:8]}  {k}  {n:3} images  -{f / MB:5.2f}MB")
        if apply_:
            if _archive_doc(d, STRIPPED_DIR) is None:
                print(f"      archive FAILED — not modified")
                continue
            col.update_one({'_id': d['_id']},
                           {'$set': {'messages': new_msgs,
                                     'images_stripped': n,
                                     'images_stripped_at': today}})
        docs += 1
        imgs += n
        freed += f
    print(f"\n  sessions {docs}   images {imgs}   freed {freed / MB:.1f} MB")
    if not apply_:
        print("  DRY RUN — nothing modified. Re-run with --apply.")
    return 0


# ---------- archive (cold-tier move) ----------

def cmd_archive(db, cutoff, apply_) -> int:
    col = db['sessions']
    print(f"  {'APPLY' if apply_ else 'DRY RUN'} — move sessions synced before {cutoff} "
          f"to the cold tier\n")
    n = freed = 0
    for d in col.find({}):
        k = _month_key(d)
        if not k or k >= cutoff:
            continue
        size = len(json_util.dumps(d).encode('utf-8'))
        print(f"    {d['session_id'][:8]}  {k}  {size / MB:6.2f}MB  "
              f"{str(d.get('project', ''))[-34:]}")
        if apply_:
            if _archive_doc(d, ARCHIVED_DIR) is None:
                print("      archive FAILED — not deleted")
                continue
            col.delete_one({'_id': d['_id']})
        n += 1
        freed += size
    print(f"\n  sessions {n}   freed {freed / MB:.1f} MB")
    if not apply_:
        print("  DRY RUN — nothing deleted. Re-run with --apply.")
    return 0


# ---------- restore ----------

def cmd_restore(db, sid) -> int:
    col = db['sessions']
    hits = [p for d in (ARCHIVED_DIR, STRIPPED_DIR) if d.is_dir()
            for p in d.glob(f'session-{sid}*.json')]
    if not hits:
        print(f"  no archive found for {sid} under {ARCHIVE_ROOT}")
        return 1
    if len(hits) > 1:
        print(f"  ambiguous — {len(hits)} archives match {sid}:")
        for p in hits:
            print(f"    {p}")
        return 1
    doc = json_util.loads(hits[0].read_text(encoding='utf-8'))
    existing = col.find_one({'session_id': doc['session_id']}, {'_id': 1})
    if existing:
        col.replace_one({'_id': existing['_id']}, doc)
        print(f"  replaced in-place: {doc['session_id']}  ({hits[0].name})")
    else:
        col.insert_one(doc)
        print(f"  re-inserted: {doc['session_id']}  ({hits[0].name})")
    print(f"  messages restored: {len(doc.get('messages') or []):,}")
    return 0


def cmd_list_archive() -> int:
    total = 0
    for label, d in (('archived (removed from mongo)', ARCHIVED_DIR),
                     ('stripped (images only)', STRIPPED_DIR)):
        files = sorted(d.glob('*.json')) if d.is_dir() else []
        size = sum(f.stat().st_size for f in files)
        total += size
        print(f"  {label:32} {len(files):4} files  {size / MB:8.1f} MB")
    print(f"  {'TOTAL':32} {'':4}        {total / MB:8.1f} MB   {ARCHIVE_ROOT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--strip', action='store_true')
    ap.add_argument('--archive', action='store_true')
    ap.add_argument('--restore', metavar='SESSION_ID')
    ap.add_argument('--list-archive', action='store_true')
    ap.add_argument('--before', metavar='YYYY-MM',
                    help=f'cutoff month (default: keep {DEFAULT_HOT_MONTHS} months hot)')
    ap.add_argument('--apply', action='store_true',
                    help='actually write; without it --strip/--archive are dry runs')
    args = ap.parse_args()

    if args.list_archive:
        return cmd_list_archive()

    client, db = get_db()
    try:
        if args.status:
            return cmd_status(db)
        if args.restore:
            return cmd_restore(db, args.restore)
        cutoff = args.before or default_cutoff()
        if args.strip:
            return cmd_strip(db, cutoff, args.apply)
        if args.archive:
            return cmd_archive(db, cutoff, args.apply)
        ap.print_help()
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())
