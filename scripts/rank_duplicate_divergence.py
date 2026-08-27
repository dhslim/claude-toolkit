"""Rank sibling sessions by how much they'd actually cost to delete.

The number that matters is not "unique vs the biggest sibling" but "unique vs
EVERY other sibling combined" -- if a message survives in any sibling, deleting
this one loses nothing. And only real user/assistant text counts: a differing
tool_result or attachment uuid is not content worth keeping a 10 MB document for.

Output is sorted by that cost, so the cut line is a judgement about a number
rather than a guess about a shape.
"""
import sys
from collections import defaultdict

sys.path.insert(0, r'C:/Users/user/Desktop/claude-toolkit')
from _shared import get_db          # noqa: E402
from bson import json_util          # noqa: E402

MB = 1048576


def text_of(m):
    ct = m.get('content')
    if isinstance(ct, list):
        return ''.join(b.get('text', '') for b in ct
                       if isinstance(b, dict) and b.get('type') == 'text')
    return ct if isinstance(ct, str) else ''


def real_uuids(doc):
    """uuids of messages that are actual conversation, not tool/metadata rows."""
    out = set()
    for m in doc.get('messages') or []:
        if (m.get('uuid') and m.get('role') in ('user', 'assistant')
                and text_of(m).strip()):
            out.add(m['uuid'])
    return out


client, db = get_db()
col = db['sessions']

groups = defaultdict(list)
for d in col.find({}):
    k = (str(d.get('session_started_at') or '')[:19], d.get('project'))
    if k[0]:
        groups[k].append(d)
dupes = {k: v for k, v in groups.items() if len(v) > 1}

rows = []
for (start, project), docs in dupes.items():
    ru = {d['session_id']: real_uuids(d) for d in docs}
    for d in docs:
        sid = d['session_id']
        others = set()
        for o in docs:
            if o['session_id'] != sid:
                others |= ru[o['session_id']]
        cost = len(ru[sid] - others)          # real messages lost if deleted
        rows.append({
            'sid': sid,
            'cost': cost,
            'mb': len(json_util.dumps(d).encode('utf-8')) / MB,
            'msgs': len(d.get('messages') or []),
            'synced': str(d.get('last_synced_at_kst'))[:10],
            'proj': str(project).split('\\')[-1][:26],
            'group': start,
        })

rows.sort(key=lambda r: (r['cost'], -r['mb']))

print(f"  {'session':10} {'cost':>6} {'MB':>6} {'msgs':>6} {'synced':11} project")
print(f"  {'-'*62}")
cum = 0
for r in rows:
    if r['cost'] == 0:
        cum += r['mb']
    flag = '  <- zero cost' if r['cost'] == 0 else ''
    print(f"  {r['sid'][:8]:10} {r['cost']:6} {r['mb']:6.1f} {r['msgs']:6} "
          f"{r['synced']:11} {r['proj']}{flag}")

print()
z = [r for r in rows if r['cost'] == 0]
print(f'  ZERO-COST deletions : {len(z)} session(s), {cum:.1f} MB')
print('  (every real message in them survives in a sibling)')
print()
for thresh in (5, 20, 50):
    sel = [r for r in rows if r['cost'] <= thresh]
    print(f'  cost <= {thresh:3}: {len(sel):2} sessions, '
          f'{sum(r["mb"] for r in sel):6.1f} MB, '
          f'{sum(r["cost"] for r in sel):5} real messages lost')

client.close()
