#!/usr/bin/env python3
"""Save generated quiz JSON from stdin to MongoDB."""

import json
import sys
from datetime import datetime, timezone

from _shared import get_db, today_kst


def main():
    data = sys.stdin.read().strip()
    if not data:
        print('No quiz data provided on stdin.', file=sys.stderr)
        sys.exit(1)

    try:
        quiz_data = json.loads(data)
    except json.JSONDecodeError as e:
        print(f'Invalid JSON: {e}', file=sys.stderr)
        sys.exit(1)

    client, db = get_db()
    try:
        doc = {
            'date': today_kst(),
            'created_at': datetime.now(timezone.utc),
            'questions': quiz_data.get('questions', []),
            'answers': quiz_data.get('answers'),
            'score': quiz_data.get('score'),
            'graded': False,
        }
        result = db['daily-quizzes'].insert_one(doc)
        print(f'Quiz saved: {result.inserted_id}')
    except Exception as e:
        print(f'Failed to save quiz: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
