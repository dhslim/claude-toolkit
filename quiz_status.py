#!/usr/bin/env python3
"""Print today's quiz status from MongoDB as one-line JSON — the source-of-truth
guard the assistant runs BEFORE presenting/regenerating/grading a quiz.

Verdicts (stdout, one JSON line):
    {"status": "taken", "score": 4, "total": 5, "quiz_id": "..."}
    {"status": "dismissed"}
    {"status": "pending", "quiz_id": "<ungraded quiz id or null>"}
    {"status": "unknown"}   ← MongoDB unreachable

FAIL SAFE: on any error (unreachable DB, bad env, etc.) print "unknown" and
exit 0. Never crash or block — a missed check is harmless, a false verdict is not.

Usage:
    python quiz_status.py
"""

import json
import sys

from _shared import today_kst, get_db_fast


def main():
    # Everything is wrapped so a MongoDB/config failure degrades to "unknown"
    # rather than crashing the caller. Short timeout keeps this snappy for a hook.
    try:
        today = today_kst()
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
        finally:
            client.close()
    except Exception as e:
        print(f'[quiz_status] MongoDB check failed; reporting unknown: '
              f'{type(e).__name__}: {e}', file=sys.stderr)
        print(json.dumps({"status": "unknown"}))
        return

    taken = bool(marker and marker.get('taken_at')) or bool(graded)
    dismissed = bool(marker and marker.get('dismissed_at'))

    if taken:
        out = {"status": "taken"}
        if graded:
            out["score"] = graded.get('score')
            out["total"] = graded.get('total') or len(graded.get('questions', []))
            out["quiz_id"] = str(graded.get('_id'))
        print(json.dumps(out))
        return

    if dismissed:
        print(json.dumps({"status": "dismissed"}))
        return

    qid = str(pending['_id']) if pending else None
    print(json.dumps({"status": "pending", "quiz_id": qid}))


if __name__ == '__main__':
    main()
