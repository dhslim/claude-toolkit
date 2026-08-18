#!/usr/bin/env python3
"""Print today's quiz status as one-line JSON — the source-of-truth guard the
assistant runs BEFORE presenting/regenerating/grading a quiz.

Reads BOTH state stores and ORs them, mirroring quiz_check.py:31-41:
  * local marker files (quiz-last-taken.txt / quiz-last-dismissed.txt)
  * MongoDB (quiz-markers / daily-quizzes)

Why both: the empty-day auto-dismiss in quiz_check.py writes the LOCAL file only
and never reaches MongoDB (there is no local->mongo sync path anywhere). A
MongoDB-only read therefore reports "pending" on a day that was already dismissed
hours earlier, which is how a spurious quiz gets generated.

Verdicts (stdout, one JSON line):
    {"status": "taken", "source": "...", "score": 4, "total": 5, "quiz_id": "..."}
    {"status": "dismissed", "source": "local|mongo|local+mongo"}
    {"status": "pending", "quiz_id": "<ungraded quiz id or null>"}
    {"status": "unknown"}   ← MongoDB unreachable AND both local markers silent

FAIL SAFE: on any error (unreachable DB, bad env, etc.) fall back to the local
markers; only when those are silent too report "unknown" and exit 0. Never crash
or block. Callers MUST treat "unknown" as stand down — it means "could not
determine", not "permission to generate".

Usage:
    python quiz_status.py
"""

import json
import sys
from pathlib import Path

from _shared import today_kst, get_db_fast

SCRIPT_DIR = Path(__file__).resolve().parent
TAKEN_FILE = SCRIPT_DIR / 'quiz-last-taken.txt'
DISMISSED_FILE = SCRIPT_DIR / 'quiz-last-dismissed.txt'


def _local_marker(path):
    """Return the marker file's date string, or '' if absent/unreadable.

    Broader than quiz_check.py's FileNotFoundError catch on purpose: a corrupted
    (non-UTF-8) marker raises UnicodeDecodeError, which is a ValueError, not an
    OSError — uncaught, it would crash the guard that exists to never crash.
    """
    try:
        return path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return ''


def _source(from_local, from_mongo):
    """Label which store produced the verdict, for diagnosability."""
    if from_local and from_mongo:
        return 'local+mongo'
    return 'local' if from_local else 'mongo'


def compute_status():
    """Return today's verdict as a dict.

    Shared by this script's CLI output and by quiz_save.py's fail-closed guard,
    so the two can never disagree about whether today was already dismissed.
    """
    today = today_kst()

    # Local markers FIRST — authoritative for this machine, and the only record of
    # an empty-day auto-dismiss, which quiz_check.py writes local-only. Read these
    # before MongoDB so the verdict survives an unreachable database.
    local_taken = _local_marker(TAKEN_FILE) == today
    local_dismissed = _local_marker(DISMISSED_FILE) == today

    marker = graded = pending = None
    mongo_ok = False
    # Wrapped so a MongoDB/config failure degrades to the local markers rather
    # than crashing the caller. Short timeout keeps this snappy for a hook.
    try:
        client, db = get_db_fast(timeout_ms=3000)
        try:
            marker = db['quiz-markers'].find_one({'date': today})
            # A quiz can be GRADED (score written to daily-quizzes) before/without
            # the marker's taken_at landing — treat any graded quiz today as taken.
            graded = db['daily-quizzes'].find_one(
                {'date': today, 'score': {'$ne': None}})
            # Earliest ungraded quiz for today, if one exists — so a pending verdict
            # points the caller at THAT quiz instead of generating a duplicate.
            pending = db['daily-quizzes'].find_one(
                {'date': today, 'score': None}, sort=[('created_at', 1)])
            mongo_ok = True
        finally:
            client.close()
    except Exception as e:
        print(f'[quiz_status] MongoDB check failed; falling back to local markers: '
              f'{type(e).__name__}: {e}', file=sys.stderr)

    mongo_taken = bool(marker and marker.get('taken_at')) or bool(graded)
    mongo_dismissed = bool(marker and marker.get('dismissed_at'))

    # OR across both stores — same semantics as quiz_check.py, so the nagger and
    # this guard can never reach opposite verdicts from the same machine state.
    taken = mongo_taken or local_taken
    dismissed = mongo_dismissed or local_dismissed

    if taken:
        out = {"status": "taken", "source": _source(local_taken, mongo_taken)}
        if graded:
            out["score"] = graded.get('score')
            out["total"] = graded.get('total') or len(graded.get('questions', []))
            out["quiz_id"] = str(graded.get('_id'))
        return out

    if dismissed:
        return {"status": "dismissed",
                "source": _source(local_dismissed, mongo_dismissed)}

    # Neither store reports taken/dismissed. If MongoDB was unreachable we truly
    # cannot tell — report "unknown" (= stand down) instead of "pending", which
    # the caller would read as permission to generate.
    if not mongo_ok:
        return {"status": "unknown"}

    qid = str(pending['_id']) if pending else None
    return {"status": "pending", "quiz_id": qid}


def main():
    print(json.dumps(compute_status()))


if __name__ == '__main__':
    main()
