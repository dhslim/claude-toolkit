#!/usr/bin/env python3
"""session_unname.py — remove a Claude Code session's name, clearing the cyan badge.

WHAT THE BADGE IS
    After `/rename`, a session wears a cyan badge with its name above the input
    box. `/rename` has no inverse: there is no `/unname`. This script performs the
    documented revert.

THE THREE STORES (this is the whole difficulty)
    The name looks like it lives in one place. It lives in three, with three
    lifetimes and three readers:

      1. ~/.claude/sessions/<pid>.json          ephemeral, per-pid
         The session RECORD. Deleted when the process exits; it is a discovery
         registry for OTHER processes, not what the TUI reads to draw its badge.
         -> needs no intervention: it is recreated on resume with a derived name.

      2. ~/.claude/projects/<slug>/<sid>.jsonl  persistent, per-session
         The TRANSCRIPT. Carries the name across restarts via two entry types:
             {"type":"custom-title","customTitle":"<name>","sessionId":"..."}
             {"type":"agent-name","agentName":"<name>","sessionId":"..."}
         Re-stamped EVERY TURN while the session is explicitly named — which is
         why stripping a LIVE session lasts only seconds.
         -> stripped by this script.

      3. ~/.claude/jobs/<first8-of-sid>/state.json   persistent, DAEMON ONLY
         The JOB STATE (a supervisor's durable state: respawnFlags, lifecycle,
         fan-out, labels). Survives process death and transcript strips, and
         re-applies the name on respawn:
             ...c.name && { name: c.name, nameSource: c.nameSource },
         Exists only for sessions BORN as background jobs (origin, not usage —
         a session created headless keeps this forever, even after a terminal is
         attached and it is used as an ordinary chat for days).
         -> `name` + `nameSource` keys removed by this script; all other keys are
            left untouched.

    Missing store 3 is what made two otherwise-correct attempts fail silently.

nameSource HAS THREE VALUES, NOT TWO
    "user" (explicit /rename, recorded in job state), "auto", "derived"
    (slug(cwd) + short suffix). In the session RECORD an explicit name omits the
    key entirely; a derived name sets nameSource="derived". Post-revert a healthy
    session looks exactly like a fresh one: name=<slug>-<xx>, nameSource=derived.

WHY LINES ARE JSON-PARSED, NEVER GREPPED
    `grep -c custom-title` over a transcript counts every message in which the
    words were merely DISCUSSED. During the original investigation that produced
    a false "the process re-stamps every turn" conclusion. This script parses
    each line and compares obj["type"] exactly.

USAGE
    python session_unname.py --list                  # which sessions carry a name
    python session_unname.py <sid> --analyze         # report all 3 stores, write nothing
    python session_unname.py <sid>                   # do the revert (refuses if live)
    python session_unname.py <sid> --force           # skip the liveness guard

    Then:  claude --resume <sid>
    (NOT --continue: that picks the most recent session in the folder and may
     grab the wrong one.)

ORDER MATTERS
    /exit the session FIRST. Liveness must be judged by PID, not by absence of a
    session record: records are per-pid and deleted on exit, so a record can be
    gone while a different pid still holds the session.

CONSIDER NOT DOING THIS
    The badge is cosmetic; the NAME is not — it is injected into the model's
    context each turn as a hint about the session's purpose. If the name is merely
    WRONG, `/rename <something-truthful>` is the better fix: one command, no file
    surgery. Use this script when you genuinely want NO name.

CAVEAT
    The scrollback still contains the literal `/rename` line you typed. The rename
    stays visible in history; it is simply no longer in effect.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_HOME = Path.home() / '.claude'
CLAUDE_PROJECTS = CLAUDE_HOME / 'projects'
CLAUDE_SESSIONS = CLAUDE_HOME / 'sessions'
CLAUDE_JOBS = CLAUDE_HOME / 'jobs'

# The two transcript entry types that carry an explicit session name.
NAME_TYPES = ('custom-title', 'agent-name')
# Cheap prefilter before the (correct but slower) JSON parse.
_PREFILTER = tuple(NAME_TYPES)
# Keys removed from job state — and ONLY these two.
JOB_NAME_KEYS = ('name', 'nameSource')

# Windows: keep console-app children (tasklist) from flashing a console window
# when this runs under pythonw.exe, which has no console of its own.
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0


# ---------- resolution ----------

def resolve_transcript(arg: str) -> Path:
    """Accept a direct path or a session-id (or unique substring)."""
    p = Path(arg)
    if p.is_file():
        return p
    matches = sorted(CLAUDE_PROJECTS.rglob(f'*{arg}*.jsonl'))
    matches = [m for m in matches if not m.name.endswith('.bak')]
    if not matches:
        sys.exit(f'No session transcript found matching: {arg}')
    if len(matches) > 1:
        listing = '\n'.join(f'  {m}' for m in matches[:10])
        sys.exit(f'Ambiguous session id {arg!r} — {len(matches)} matches:\n{listing}')
    return matches[0]


def session_id_of(transcript: Path) -> str:
    return transcript.stem


def job_state_path(sid: str) -> Path:
    """Job state lives under the first 8 chars of the session id (daemonShort)."""
    return CLAUDE_JOBS / sid[:8] / 'state.json'


# ---------- liveness ----------

def pid_alive(pid: int) -> bool:
    if sys.platform == 'win32':
        try:
            out = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
            ).stdout
        except Exception:
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def holders_of(sid: str) -> list[tuple[int, Path]]:
    """Live pids whose session record claims this session id."""
    found = []
    if not CLAUDE_SESSIONS.is_dir():
        return found
    for rec in CLAUDE_SESSIONS.glob('*.json'):
        try:
            obj = json.loads(rec.read_text(encoding='utf-8'))
        except Exception:
            continue
        if obj.get('sessionId') != sid:
            continue
        pid = obj.get('pid')
        if isinstance(pid, int) and pid_alive(pid):
            found.append((pid, rec))
    return found


def seconds_since_write(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return float('inf')


# ---------- inspection ----------

def scan_transcript(transcript: Path) -> dict:
    """Count real name entries (JSON-parsed, never grepped) and the names seen."""
    counts = {t: 0 for t in NAME_TYPES}
    names, mentions, total = set(), 0, 0
    with transcript.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            total += 1
            if not any(k in line for k in _PREFILTER):
                continue
            mentions += 1
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            t = obj.get('type')
            if t in counts:
                counts[t] += 1
                nm = obj.get('customTitle') or obj.get('agentName')
                if nm:
                    names.add(nm)
    return {
        'counts': counts,
        'real': sum(counts.values()),
        'lines': total,
        # prefilter hits that were NOT real entries = the words merely discussed
        'false_positives': mentions - sum(counts.values()),
        'names': sorted(names),
    }


def read_job_state(sid: str):
    p = job_state_path(sid)
    if not p.is_file():
        return None, None
    try:
        return p, json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'  ! job state unreadable ({e}) — skipping store 3', file=sys.stderr)
        return p, None


# ---------- mutation ----------

def strip_transcript(transcript: Path, backup: bool) -> int:
    """Drop every real name entry. Unparseable lines pass through verbatim."""
    if backup:
        bak = transcript.with_name(
            transcript.name + f'.bak-unname-{time.strftime("%Y%m%d-%H%M%S")}')
        shutil.copy2(transcript, bak)
        print(f'  backup: {bak}')

    tmp = transcript.with_suffix(transcript.suffix + '.tmp')
    removed = 0
    with transcript.open('r', encoding='utf-8', errors='replace') as fi, \
         tmp.open('w', encoding='utf-8', newline='\n') as fo:
        for line in fi:
            if any(k in line for k in _PREFILTER):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get('type') in NAME_TYPES:
                        removed += 1
                        continue
                except Exception:
                    pass
            fo.write(line)
    os.replace(tmp, transcript)
    return removed


def clean_job_state(path: Path, state: dict, backup: bool) -> list[str]:
    """Remove ONLY name/nameSource; every other key is preserved as-is."""
    present = [k for k in JOB_NAME_KEYS if k in state]
    if not present:
        return []
    if backup:
        bak = path.with_name(
            path.name + f'.bak-unname-{time.strftime("%Y%m%d-%H%M%S")}')
        shutil.copy2(path, bak)
        print(f'  backup: {bak}')
    for k in present:
        state.pop(k, None)
    tmp = path.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(state, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)
    return present


# ---------- commands ----------

def cmd_list() -> int:
    """Every session whose transcript carries a real name entry."""
    rows = []
    for tr in CLAUDE_PROJECTS.rglob('*.jsonl'):
        if tr.name.endswith('.bak') or '.bak-' in tr.name:
            continue
        info = scan_transcript(tr)
        if info['real'] == 0:
            continue
        sid = session_id_of(tr)
        rows.append((sid, info, job_state_path(sid).is_file(), tr))
    if not rows:
        print('No session carries an explicit name — nothing has a badge.')
        return 0
    rows.sort(key=lambda r: -r[1]['real'])
    print(f'{len(rows)} session(s) carrying an explicit name:\n')
    print(f'{"session":10} {"entries":>8} {"jobs":>5}  name(s) / project')
    for sid, info, has_job, tr in rows:
        nm = ', '.join(info['names']) or '?'
        print(f'{sid[:8]:10} {info["real"]:>8} {"YES" if has_job else "-":>5}  '
              f'{nm}   [{tr.parent.name}]')
    print('\nStores: "jobs YES" = daemon-backed, needs store 3 cleaned too.')
    print('Revert one with:  python session_unname.py <session>')
    return 0


def cmd_report(transcript: Path, sid: str) -> dict:
    info = scan_transcript(transcript)
    jp, state = read_job_state(sid)
    holders = holders_of(sid)
    age = seconds_since_write(transcript)

    print(f'session      {sid}')
    print(f'transcript   {transcript}')
    print(f'  lines={info["lines"]}  name entries={info["real"]} '
          f'(custom-title={info["counts"]["custom-title"]}, '
          f'agent-name={info["counts"]["agent-name"]})')
    if info['false_positives']:
        print(f'  ({info["false_positives"]} line(s) merely MENTION those words — '
              f'not counted; this is why grep over-counts)')
    print(f'  name(s) stamped: {", ".join(info["names"]) or "(none)"}')
    print(f'  last written {age:.0f}s ago')

    if state is None:
        print(f'jobs state   none  ({job_state_path(sid)})')
        print('             -> 2 stores; store 3 step not applicable')
    else:
        have = [k for k in JOB_NAME_KEYS if k in state]
        print(f'jobs state   {jp}')
        print(f'  backend={state.get("backend")} template={state.get("template")} '
              f'state={state.get("state")}')
        print(f'  name={state.get("name", "<ABSENT>")!r} '
              f'nameSource={state.get("nameSource", "<ABSENT>")!r}')
        print(f'  keys total={len(state)}  to remove={have or "none"} '
              f'(all others preserved)')
        print('             -> 3 stores; DAEMON-BACKED, store 3 is required')

    if holders:
        for pid, rec in holders:
            print(f'LIVE         pid {pid} still holds this session ({rec.name})')
    else:
        print('LIVE         no live pid claims this session')
    return {'info': info, 'jp': jp, 'state': state, 'holders': holders, 'age': age}


def cmd_revert(transcript: Path, sid: str, args) -> int:
    print('=== before ===')
    st = cmd_report(transcript, sid)
    holders, info, state, jp = st['holders'], st['info'], st['state'], st['jp']

    if info['real'] == 0 and not (state and any(k in state for k in JOB_NAME_KEYS)):
        print('\nNothing to do: no explicit name in any store (badge already clear).')
        return 0

    if holders and not args.force:
        pids = ', '.join(str(p) for p, _ in holders)
        print(f'\nREFUSING: session is LIVE (pid {pids}).', file=sys.stderr)
        print('A named session re-stamps the transcript every turn, so a strip now '
              'would last seconds.\n/exit that session first, then re-run. '
              '(--force overrides.)', file=sys.stderr)
        return 2
    if not holders and st['age'] < 20 and not args.force:
        print(f'\nREFUSING: transcript was written {st["age"]:.0f}s ago — the session '
              'looks active even though no record claims it.', file=sys.stderr)
        print('Records are per-pid and deleted on exit, so a live session can have no '
              'record. Wait, or pass --force.', file=sys.stderr)
        return 2

    if not args.yes:
        print(f'\nAbout to strip {info["real"]} transcript entr(y/ies)'
              + (f' and {len([k for k in JOB_NAME_KEYS if k in (state or {})])} '
                 f'job-state key(s)' if state else '')
              + '.')
        try:
            if input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
                print('Aborted.')
                return 1
        except EOFError:
            print('Aborted (no tty; pass --yes).')
            return 1

    print('\n=== acting ===')
    removed = strip_transcript(transcript, backup=not args.no_backup)
    print(f'  transcript: removed {removed} name entr(y/ies)')
    if state is not None:
        gone = clean_job_state(jp, state, backup=not args.no_backup)
        print(f'  job state:  removed {gone or "nothing"} '
              f'(other keys untouched)')

    print('\n=== after ===')
    after = scan_transcript(transcript)
    _, state2 = read_job_state(sid)
    print(f'  transcript name entries: {after["real"]}')
    if state2 is not None:
        print(f'  job state name/nameSource: '
              f'{[k for k in JOB_NAME_KEYS if k in state2] or "ABSENT"} '
              f'({len(state2)} keys intact)')
    ok = after['real'] == 0 and not (state2 and any(k in state2 for k in JOB_NAME_KEYS))
    print(f'\n{"OK — all stores clear." if ok else "WARNING — something remains."}')
    print(f'Now resume it:   claude --resume {sid}')
    print('(not --continue — that may grab a different session)')
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Remove a Claude Code session's name (clears the cyan badge).")
    ap.add_argument('session', nargs='?',
                    help='session id (or unique substring), or transcript path')
    ap.add_argument('--list', action='store_true',
                    help='list sessions that carry an explicit name, then exit')
    ap.add_argument('--analyze', action='store_true',
                    help='report all three stores and write nothing')
    ap.add_argument('--yes', '-y', action='store_true', help='skip the confirmation')
    ap.add_argument('--force', action='store_true',
                    help='proceed even if the session looks live (unsafe)')
    ap.add_argument('--no-backup', action='store_true', help='skip .bak copies')
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if not args.session:
        ap.error('give a session id, or --list')

    transcript = resolve_transcript(args.session)
    sid = session_id_of(transcript)
    if args.analyze:
        cmd_report(transcript, sid)
        return 0
    return cmd_revert(transcript, sid, args)


if __name__ == '__main__':
    sys.exit(main())
