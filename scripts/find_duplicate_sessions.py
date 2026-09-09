"""Find duplicate sessions and classify them. READ-ONLY -- deletes nothing.

The safe question is not "do these look similar" but "does deleting this lose
anything". Message uuids answer it exactly:

    A subset of B   -> A adds nothing, safe to drop A
    identical       -> either one, drop one
    diverged        -> each holds messages the other does not; DO NOT delete

Sessions forked with /bg or resumed in a worktree share a common prefix and then
diverge, so message COUNT alone is not evidence of anything -- the smaller one
can still hold unique tail messages.
"""
import sys
from collections import defaultdict

sys.path.insert(0, r'C:/Users/user/Desktop/claude-toolkit')
from _shared import get_db          # noqa: E402
from bson import json_util          # noqa: E402

MB = 1048576


def uuid_set(doc):
    return {m.get('uuid') for m in (doc.get('messages') or []) if m.get('uuid')}


def size_mb(doc):
    return len(json_util.dumps(doc).encode('utf-8')) / MB


client, db = get_db()
col = db['sessions']

# Group by (start time, project). Same conversation resumed/forked keeps both.
groups = defaultdict(list)
for d in col.find({}):
    key = (str(d.get('session_started_at') or '')[:19], d.get('project'))
    if not key[0]:
        continue
    groups[key].append(d)

dupes = {k: v for k, v in groups.items() if len(v) > 1}
print(f'  {len(dupes)} group(s) sharing a start time + project\n')

safe_bytes = 0
safe_list = []

for (start, project), docs in sorted(dupes.items()):
    docs.sort(key=lambda d: -len(d.get('messages') or []))
    print(f'  {start}  {str(project)[-46:]}')
    sets = {d['session_id']: uuid_set(d) for d in docs}

    for d in docs:
        sid = d['session_id']
        mine = sets[sid]
        # is this doc's uuid set contained in ANY other doc in the group?
        covered_by = [o['session_id'][:8] for o in docs
                      if o['session_id'] != sid and mine and mine <= sets[o['session_id']]]
        uniq = len(mine - set().union(*[sets[o['session_id']] for o in docs
                                        if o['session_id'] != sid]) ) if len(docs) > 1 else len(mine)
        tag = f'SUBSET of {covered_by[0]}' if covered_by else f'{uniq} unique msg(s)'
        mb = size_mb(d)
        print(f'    {sid[:8]}  {len(d.get("messages") or []):>5} msgs  {mb:6.1f}M  {tag}')
        if covered_by:
            safe_bytes += mb
            safe_list.append((sid, mb, covered_by[0]))
    print()

print(f'  SAFE to delete (strict subsets): {len(safe_list)} session(s), '
      f'{safe_bytes:.1f} MB')
for sid, mb, keeper in safe_list:
    print(f'    {sid}  {mb:.1f}M   fully contained in {keeper}')
if not safe_list:
    print('    none -- every duplicate holds messages the others do not')

client.close()
