#!/usr/bin/env python3
"""Drop extended-thinking `signature` blobs from the MongoDB warehouse.

WHAT THESE ARE
    Every extended-thinking block carries a 380-char base64 signature: a
    cryptographic seal (283 bytes; the model name "claude-opus-4-78" is the only
    readable thing inside) that lets the API verify the reasoning was not forged
    when a conversation is replayed to it.

    The reasoning TEXT is not in the transcript at all -- measured across 1,866
    thinking blocks in 12 June sessions, the `thinking` field was empty in
    1,866 of them and the signatures totalled 6.98 MB. So this deletes a seal on
    content that was never stored, not the content.

WHY IT IS WORTH 71% OF THE ARCHIVE
    A signature is base64 of cryptographic output -- maximum entropy by
    construction, since a compressible signature would be a broken one. It is
    38.7% of the raw bytes but ~71% of the COMPRESSED bytes, and it is why every
    codec converged at ~2.5x. Measured on real sessions:

        as stored today                18.6M -> 7.55M   2.46x
        drop thinking signatures       11.6M -> 2.17M   8.55x   (-71%)

WHAT THIS MUST NEVER TOUCH
    ~/.claude/projects/**.jsonl -- `claude --resume` replays those transcripts to
    the API, and the API checks the signatures. Stripping them there could break
    resumption of a live session. This script only ever writes to MongoDB, which
    nothing replays from.

USAGE
    python strip_thinking_signatures.py            # dry run
    python strip_thinking_signatures.py --apply
"""
from __future__ import annotations

import argparse
import lzma
import sys

from bson import json_util
from bson.binary import Binary

from _shared import force_utf8_io, get_db

force_utf8_io()

MB = 1024 * 1024
PRESET = 6


def strip(msgs) -> int:
    """Remove `signature` from thinking blocks in place. Returns bytes removed.

    The block itself is KEPT. Deleting the whole block would change the message
    structure and the block count, which readers may rely on; dropping one field
    leaves the shape intact and the field is pure overhead.
    """
    freed = 0
    for m in msgs:
        ct = m.get('content')
        if not isinstance(ct, list):
            continue
        for b in ct:
            if isinstance(b, dict) and b.get('type') == 'thinking' and 'signature' in b:
                freed += len(b['signature'])
                del b['signature']
    return freed


def do_sessions(db, apply: bool) -> tuple[int, int]:
    col = db['sessions']
    touched = freed = 0
    for d in col.find({}, {'session_id': 1, 'messages': 1}):
        msgs = d.get('messages') or []
        n = strip(msgs)
        if not n:
            continue
        freed += n
        touched += 1
        if apply:
            col.update_one({'session_id': d['session_id']},
                           {'$set': {'messages': msgs}})
    return touched, freed


def do_archive(db, apply: bool) -> tuple[int, int]:
    """Archived sessions must be decompressed, stripped, and recompressed.

    Recompression is where the real win lands: removing the incompressible
    signatures lets lzma work on what is left, so the blob shrinks by far more
    than the raw byte count removed.
    """
    arc = db['sessions-archive']
    touched = 0
    before = after = 0
    for d in arc.find({}, {'session_id': 1, 'blob': 1, 'raw_bytes': 1}):
        msgs = json_util.loads(lzma.decompress(d['blob']).decode('utf-8'))
        if not strip(msgs):
            continue
        raw = json_util.dumps(msgs).encode('utf-8')
        blob = Binary(lzma.compress(raw, preset=PRESET))
        before += len(d['blob'])
        after += len(blob)
        touched += 1
        if apply:
            # verify the new blob round-trips BEFORE replacing the old one
            if json_util.loads(lzma.decompress(blob).decode('utf-8')) != msgs:
                print(f"  VERIFY FAILED {d['session_id'][:8]} -- left untouched")
                after -= len(blob)
                before -= len(d['blob'])
                touched -= 1
                continue
            arc.update_one({'session_id': d['session_id']},
                           {'$set': {'blob': blob, 'raw_bytes': len(raw),
                                     'signatures_stripped': True}})
    return touched, before - after


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    client, db = get_db()
    try:
        stats = db.command('dbStats', scale=1)
        print(f"  before: dataSize {stats['dataSize'] / MB:.1f} MB, "
              f"headroom {512 - stats['dataSize'] / MB:.1f} MB")
        print(f"  MODE: {'APPLY' if a.apply else 'DRY RUN'}\n")

        t1, f1 = do_sessions(db, a.apply)
        print(f'  sessions        : {t1:3} touched, {f1 / MB:7.1f} MB of signatures')
        t2, f2 = do_archive(db, a.apply)
        print(f'  sessions-archive: {t2:3} touched, {f2 / MB:7.1f} MB saved after recompression')
        print(f'  TOTAL           : {(f1 + f2) / MB:7.1f} MB')

        if a.apply:
            stats = db.command('dbStats', scale=1)
            print(f"\n  after: dataSize {stats['dataSize'] / MB:.1f} MB, "
                  f"headroom {512 - stats['dataSize'] / MB:.1f} MB")
        else:
            print('\n  (dry run -- re-run with --apply)')
    finally:
        client.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
