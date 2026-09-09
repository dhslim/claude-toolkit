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
import io
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


def _utf8_stream(stream):
    """Pin one output stream to UTF-8, replacing anything it still cannot encode.

    THE GATE MUST NOT BE DECIDED BY A CODEPAGE. /unbadge reads this script's EXIT
    CODE as its verdict (0 = provably dead, safe to clear; 1 = live/not safe;
    2 = liveness UNKNOWN, refuse). The verdict lines carry em dashes, and session
    names and cwds can be Korean, so on a cp949 console — which is what a plain
    PowerShell or cmd gives on this machine, and what the skill's documented
    command runs with, since it sets no PYTHONUTF8 — every print() was one
    UnicodeEncodeError away from replacing the answer:

        PYTHONIOENCODING=cp949 python session_unname.py --verify 060df14e
        UnicodeEncodeError: 'cp949' codec can't encode character '\\u2014'
        EXIT=1

    An unhandled encode error exits 1, which reads as "still live, refuse" — it
    happened to degrade toward safety, but the number came from the console
    encoding rather than from session state, and a PASS could not be reported at
    all. errors='replace' guarantees output can never raise, so the exit code is
    always the one the gate logic chose. reconfigure() is the 3.7+ path; the
    TextIOWrapper rebuild covers a stream without it; a stream with neither is
    left alone rather than crashing the tool over I/O setup.
    """
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='replace')
        return stream
    if hasattr(stream, 'buffer'):
        return io.TextIOWrapper(stream.buffer, encoding='utf-8', errors='replace')
    return stream


sys.stdout = _utf8_stream(sys.stdout)
sys.stderr = _utf8_stream(sys.stderr)


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


def process_map():
    """pid -> image name for every running process, in ONE spawn.

    Returns None on failure — NOT an empty dict. The distinction is safety-critical:
    an empty dict would make every session look dead and let the gate report a false
    "all clear". Callers must treat None as UNKNOWN and refuse to proceed.
    """
    try:
        if sys.platform == 'win32':
            out = subprocess.run(
                ['tasklist', '/NH', '/FO', 'CSV'],
                capture_output=True, text=True, timeout=20,
                creationflags=_NO_WINDOW,
            )
            if out.returncode != 0 or not out.stdout.strip():
                return None
            pm = {}
            for row in out.stdout.splitlines():
                parts = [c.strip('"') for c in row.split('","')]
                if len(parts) < 2:
                    continue
                name = parts[0].lstrip('"')
                try:
                    pm[int(parts[1])] = name
                except ValueError:
                    continue
            return pm or None
        out = subprocess.run(['ps', '-eo', 'pid=,comm='],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        pm = {}
        for row in out.stdout.splitlines():
            row = row.strip().split(None, 1)
            if len(row) == 2:
                try:
                    pm[int(row[0])] = os.path.basename(row[1])
                except ValueError:
                    continue
        return pm or None
    except Exception:
        return None


def read_session_records() -> tuple[list[dict], list[Path]]:
    """Every session record on disk, plus any that could NOT be parsed.

    The unreadable list is returned, never swallowed: a corrupt or mid-write record
    could belong to a LIVE session, and silently skipping it would let the gate
    report a false "all clear". Callers must fail closed when it is non-empty.
    """
    out, bad = [], []
    if not CLAUDE_SESSIONS.is_dir():
        return out, bad
    for rec in CLAUDE_SESSIONS.glob('*.json'):
        try:
            obj = json.loads(rec.read_text(encoding='utf-8'))
        except Exception:
            bad.append(rec)
            continue
        if isinstance(obj, dict):
            obj['_record'] = rec
            out.append(obj)
        else:
            bad.append(rec)
    return out, bad


def holders_of(sid: str, pmap, records) -> list[dict]:
    """Live processes whose session record claims this session id.

    Each holder carries enough context to FIND the window: cwd, badge name, kind,
    status, pid — plus the running image name so pid reuse is visible rather than
    silently trusted. pmap=None means liveness could not be determined; holders are
    returned with alive='unknown' so the caller can refuse instead of assuming dead.

    Both arguments are REQUIRED on purpose. They used to default to None, and the
    records fallback was `read_session_records()[0]` — that `[0]` dropped the
    unreadable list on the floor, so every caller that omitted it silently judged
    liveness from a subset of the records. Omitting one is now a TypeError at the
    call site instead of a false "no live pid claims this session".
    """
    found = []
    for obj in records:
        if obj.get('sessionId') != sid:
            continue
        pid = obj.get('pid')
        if not isinstance(pid, int):
            continue
        if pmap is None:
            alive, image = 'unknown', '?'
        else:
            image = pmap.get(pid, '')
            alive = bool(image)
        found.append({
            'pid': pid, 'alive': alive, 'image': image,
            'cwd': obj.get('cwd', '?'), 'name': obj.get('name', '?'),
            'nameSource': obj.get('nameSource'), 'kind': obj.get('kind', '?'),
            'status': obj.get('status', '?'), 'version': obj.get('version', '?'),
            'record': obj.get('_record'),
            # pid reuse tell: the pid exists but is NOT a claude process
            'suspect_reuse': bool(pmap is not None and image
                                  and 'claude' not in image.lower()),
        })
    return [h for h in found if h['alive'] is not False]


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

def collect_named(pmap, records, only=None) -> list[dict]:
    """Every session carrying an explicit name, from BOTH persistent stores.

    Unions two scans, because either store alone can hold a name:
      - transcripts with custom-title / agent-name entries
      - job states with a name / nameSource key (a stripped transcript still gets
        its badge back on respawn from here, so these must not be missed)
    """
    found: dict[str, dict] = {}

    for tr in CLAUDE_PROJECTS.rglob('*.jsonl'):
        if tr.name.endswith('.bak') or '.bak-' in tr.name:
            continue
        sid = session_id_of(tr)
        if only and not any(sid.startswith(o) or o in sid for o in only):
            continue
        info = scan_transcript(tr)
        jp = job_state_path(sid)
        job_keys, job_name = [], None
        if jp.is_file():
            _, st = read_job_state(sid)
            if st:
                job_keys = [k for k in JOB_NAME_KEYS if k in st]
                job_name = st.get('name')
        if info['real'] == 0 and not job_keys:
            continue
        found[sid] = {
            'sid': sid, 'transcript': tr, 'entries': info['real'],
            'names': info['names'] or ([job_name] if job_name else []),
            'has_job': jp.is_file(), 'job_keys': job_keys,
            'project': tr.parent.name, 'age': seconds_since_write(tr),
        }

    # job states whose transcript is missing/elsewhere
    if CLAUDE_JOBS.is_dir():
        for st_path in CLAUDE_JOBS.glob('*/state.json'):
            try:
                st = json.loads(st_path.read_text(encoding='utf-8'))
            except Exception:
                continue
            keys = [k for k in JOB_NAME_KEYS if k in st]
            sid = st.get('sessionId')
            if not keys or not isinstance(sid, str) or sid in found:
                continue
            if only and not any(sid.startswith(o) or o in sid for o in only):
                continue
            found[sid] = {
                'sid': sid, 'transcript': None, 'entries': 0,
                'names': [st.get('name')] if st.get('name') else [],
                'has_job': True, 'job_keys': keys,
                'project': '(no transcript found)', 'age': float('inf'),
            }

    rows = list(found.values())
    for r in rows:
        r['holders'] = holders_of(r['sid'], pmap=pmap, records=records)
        r['live'] = bool(r['holders'])
    rows.sort(key=lambda r: (not r['live'], -r['entries']))
    return rows


def _fmt_live(row) -> str:
    if not row['holders']:
        return 'dead'
    if any(h['alive'] == 'unknown' for h in row['holders']):
        return 'UNKNOWN'
    return 'LIVE ' + ','.join(str(h['pid']) for h in row['holders'])


def cmd_plan(only=None) -> int:
    """The guided report: what carries a name, what must be exited, then the gate."""
    pmap = process_map()
    records, bad_records = read_session_records()
    rows = collect_named(pmap, records, only=only)

    print('=' * 72)
    print('STEP 1 — SESSIONS CARRYING AN EXPLICIT NAME')
    print('=' * 72)
    if not rows:
        print('None. Nothing carries a badge; there is nothing to do.')
        return 0
    print(f'{"session":10} {"entries":>7}  {"stores":<9} {"live":<14} name / project')
    for r in rows:
        stores = '3 daemon' if r['has_job'] else '2'
        print(f'{r["sid"][:8]:10} {r["entries"]:>7}  {stores:<9} {_fmt_live(r):<14} '
              f'{", ".join(n for n in r["names"] if n) or "?"}  [{r["project"]}]')

    live_rows = [r for r in rows if r['live']]
    unknown = pmap is None

    print()
    print('=' * 72)
    print('STEP 2 — LIVE HOLDERS: EXIT THESE FIRST')
    print('=' * 72)
    if bad_records:
        print(f'WARNING: {len(bad_records)} session record(s) could not be parsed:')
        for b in bad_records:
            print(f'  {b}')
        print('Any of them could belong to a LIVE session, so liveness is not')
        print('trustworthy until they are readable or removed. The gate will refuse.')
        print()
    if unknown:
        print('COULD NOT PROBE RUNNING PROCESSES — liveness is UNKNOWN.')
        print('Refusing to declare anything safe. Fix the process probe, then re-run.')
    elif not live_rows:
        print('No live process holds any named session. Nothing to exit.')
    else:
        print(f'{len(live_rows)} session(s) are held by a live process. A named session')
        print('re-stamps its transcript EVERY TURN, so clearing it now would last')
        print('seconds. Exit each of these, then run the gate in step 3.\n')
        for r in live_rows:
            for h in r['holders']:
                print(f'  session {r["sid"][:8]}  pid {h["pid"]} ({h["image"]})')
                print(f'      badge   {h["name"]}')
                print(f'      window  cwd={h["cwd"]}')
                print(f'      state   {h["kind"]}, {h["status"]}, cc {h["version"]}')
                if h['suspect_reuse']:
                    print('      NOTE    image is not claude -> probably a REUSED pid;'
                          ' this session may already be dead')
                elif h['kind'] != 'interactive':
                    print('      ACTION  not an interactive terminal — stop the '
                          'background/daemon job (claude agents), do not just /exit')
                else:
                    print('      ACTION  switch to that window and type  /exit')
                print()
        print('If one of these is the session you are reading this in, you cannot')
        print('clear it from inside — it is live by definition.')

    print('=' * 72)
    print('STEP 3 — GATE: re-verify before touching any file')
    print('=' * 72)
    targets = ' '.join(r['sid'][:8] for r in rows)
    print(f'  session_unname.py --verify {targets}')
    print('  Exit code 0 = every target is dead and safe to clear.')
    print('  It re-probes live processes and matches the running image, so a recycled')
    print('  pid is not mistaken for a live session.')

    print()
    print('=' * 72)
    print('STEP 4 — CLEAR (once the gate passes)')
    print('=' * 72)
    for r in rows:
        print(f'  session_unname.py {r["sid"][:8]} --yes'
              + ('   # 3 stores: transcript + job state' if r['has_job'] else ''))

    print()
    print('=' * 72)
    print('STEP 5 — BRING EACH SESSION BACK')
    print('=' * 72)
    for r in rows:
        print(f'  claude --resume {r["sid"]}')
    print('  (never --continue: it picks the most recent session in the folder)')
    return 0


def cmd_verify(only=None) -> int:
    """The gate. Exit 0 only if every target is provably dead."""
    pmap = process_map()
    records, bad_records = read_session_records()
    rows = collect_named(pmap, records, only=only)

    if pmap is None:
        print('GATE: FAIL — could not probe running processes; liveness UNKNOWN.',
              file=sys.stderr)
        return 2
    if bad_records:
        # Fail closed, but still show the report — just label it untrustworthy.
        print(f'!! {len(bad_records)} unreadable session record(s) — every "dead" below '
              f'is UNTRUSTWORTHY, because any of these could be a live session:')
        for b in bad_records:
            print(f'   {b}')
        print()
    if not rows:
        print('GATE: nothing carries a name — nothing to verify.')
        return 0

    bad = []
    for r in rows:
        live = _fmt_live(r)
        note = ''
        if r['holders'] and all(h['suspect_reuse'] for h in r['holders']):
            note = '  (pid exists but is not claude — recycled pid, treated as LIVE '
            note += 'until it clears)'
        print(f'  {r["sid"][:8]}  {live:<14} entries={r["entries"]:<4} '
              f'{"3-store" if r["has_job"] else "2-store"}{note}')
        if r['live']:
            bad.append(r)
        elif r['age'] < 20:
            print(f'      transcript written {r["age"]:.0f}s ago — looks active with no '
                  f'record; treating as NOT safe')
            bad.append(r)

    if bad_records:
        print(f'\nGATE: FAIL — liveness cannot be trusted while {len(bad_records)} '
              f'session record(s) are unreadable.', file=sys.stderr)
        print('Remove or repair them, then re-run.', file=sys.stderr)
        return 2
    if bad:
        print(f'\nGATE: FAIL — {len(bad)} session(s) not safe to clear yet.',
              file=sys.stderr)
        return 1
    print(f'\nGATE: PASS — all {len(rows)} target(s) are dead. Safe to clear.')
    return 0


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
    """All three stores plus liveness — and how much that liveness can be trusted.

    Probes ONCE and hands both inputs down, because this is the report the
    DESTRUCTIVE path reads: it needs the same two fail-closed signals the gate has —
    the process map (None = probe failed) and the unreadable-record list.
    """
    info = scan_transcript(transcript)
    jp, state = read_job_state(sid)
    pmap = process_map()
    records, bad_records = read_session_records()
    holders = holders_of(sid, pmap=pmap, records=records)
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

    if bad_records:
        print(f'records      {len(bad_records)} UNREADABLE — any of them could be a '
              f'live session, so')
        print('             the liveness line below is NOT trustworthy:')
        for b in bad_records:
            print(f'             {b}')

    if holders:
        for h in holders:
            if h['alive'] == 'unknown':
                print(f'LIVE         UNKNOWN — could not probe processes (pid {h["pid"]})')
            else:
                print(f'LIVE         pid {h["pid"]} ({h["image"]}) holds this session '
                      f'— {h["kind"]}, {h["status"]}, cwd={h["cwd"]}')
                if h['suspect_reuse']:
                    print('             ^ image is NOT claude: likely a REUSED pid, '
                          'not a live session')
    elif pmap is None:
        print('LIVE         UNKNOWN — could not probe running processes; no record '
              'claims')
        print('             this session, but that is not evidence while the probe '
              'is blind')
    else:
        print('LIVE         no live pid claims this session')
    return {'info': info, 'jp': jp, 'state': state, 'holders': holders, 'age': age,
            'pmap': pmap, 'bad_records': bad_records}


def cmd_revert(transcript: Path, sid: str, args) -> int:
    """The destructive step. Exit codes speak the gate's vocabulary:
    1 = live / not safe yet, 2 = liveness UNKNOWN, refuse."""
    print('=== before ===')
    st = cmd_report(transcript, sid)
    holders, info, state, jp = st['holders'], st['info'], st['state'], st['jp']
    pmap, bad_records = st['pmap'], st['bad_records']

    if info['real'] == 0 and not (state and any(k in state for k in JOB_NAME_KEYS)):
        print('\nNothing to do: no explicit name in any store (badge already clear).')
        return 0

    # Liveness must be TRUSTWORTHY before "no live pid claims this session" is worth
    # anything, and two conditions make it worthless: a session record that could not
    # be parsed (it could be THIS session's own, live and re-stamping) and a process
    # probe that failed (it knows nothing about any pid). --verify fails closed on
    # both with exit 2; so must the command that actually edits files.
    if not args.force:
        if bad_records:
            print(f'\nREFUSING: {len(bad_records)} session record(s) could not be '
                  f'parsed, so liveness is UNKNOWN:', file=sys.stderr)
            for b in bad_records:
                print(f'  {b}', file=sys.stderr)
            print('Any of them could be this session, held by a live pid that never '
                  'shows up above.\nRepair or remove them, then re-run. '
                  '(--force overrides.)', file=sys.stderr)
            return 2
        if pmap is None:
            print('\nREFUSING: could not probe running processes — liveness is '
                  'UNKNOWN.', file=sys.stderr)
            print('Nothing above proves this session is dead. Fix the process probe '
                  '(tasklist/ps), then re-run. (--force overrides.)', file=sys.stderr)
            return 2

    if holders and not args.force:
        pids = ', '.join(str(h['pid']) for h in holders)
        print(f'\nREFUSING: session is LIVE (pid {pids}).', file=sys.stderr)
        print('A named session re-stamps the transcript every turn, so a strip now '
              'would last seconds.\n/exit that session first, then re-run. '
              '(--force overrides.)', file=sys.stderr)
        return 1
    if not holders and st['age'] < 20 and not args.force:
        print(f'\nREFUSING: transcript was written {st["age"]:.0f}s ago — the session '
              'looks active even though no record claims it.', file=sys.stderr)
        print('Records are per-pid and deleted on exit, so a live session can have no '
              'record. Wait, or pass --force.', file=sys.stderr)
        return 1

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
    ap.add_argument('session', nargs='*',
                    help='session id(s) (or unique substring), or a transcript path')
    ap.add_argument('--plan', action='store_true',
                    help='guided report: what is named, what to exit, gate, clear, resume')
    ap.add_argument('--verify', action='store_true',
                    help='gate: exit 0 only if every target is provably dead')
    ap.add_argument('--list', action='store_true',
                    help='list sessions that carry an explicit name, then exit')
    ap.add_argument('--analyze', action='store_true',
                    help='report all three stores and write nothing')
    ap.add_argument('--yes', '-y', action='store_true', help='skip the confirmation')
    ap.add_argument('--force', action='store_true',
                    help='proceed even if the session looks live (unsafe)')
    ap.add_argument('--no-backup', action='store_true', help='skip .bak copies')
    args = ap.parse_args()

    if args.plan:
        return cmd_plan(only=args.session or None)
    if args.verify:
        return cmd_verify(only=args.session or None)
    if args.list:
        return cmd_list()
    if not args.session:
        return cmd_plan()          # bare invocation: show the guided report
    if len(args.session) > 1:
        ap.error('acting on one session at a time; pass a single id '
                 '(use --plan / --verify for multiple)')

    transcript = resolve_transcript(args.session[0])
    sid = session_id_of(transcript)
    if args.analyze:
        cmd_report(transcript, sid)
        return 0
    return cmd_revert(transcript, sid, args)


if __name__ == '__main__':
    sys.exit(main())
