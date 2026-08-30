#!/usr/bin/env python3
"""Grade a quiz: compare user answers to stored quiz, update MongoDB, mark done.

Usage (pipe JSON to stdin):
    echo '{"quiz_id": "...", "answers": ["B", "C", "A", ...]}' | python quiz_grade.py

If quiz_id is omitted, grades the most recent quiz for today.
Returns JSON with score, total, and per-question results.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from _shared import force_utf8_io, get_db, to_kst_iso, today_kst

SCRIPT_DIR = Path(__file__).resolve().parent
TAKEN_FILE = SCRIPT_DIR / 'quiz-last-taken.txt'


def main():
    force_utf8_io()
    data = sys.stdin.read().strip()
    if not data:
        print(json.dumps({"error": "No input provided on stdin."}, ensure_ascii=False))
        sys.exit(1)

    try:
        input_data = json.loads(data)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}, ensure_ascii=False))
        sys.exit(1)

    user_answers = input_data.get('answers', [])
    if not user_answers:
        print(json.dumps({"error": "No answers provided."}, ensure_ascii=False))
        sys.exit(1)

    client, db = get_db()
    try:
        today = today_kst()

        # Find the quiz document
        quiz_id = input_data.get('quiz_id')
        if quiz_id:
            quiz_doc = db['daily-quizzes'].find_one({'_id': ObjectId(quiz_id)})
        else:
            # Most recent quiz for today
            quiz_doc = db['daily-quizzes'].find_one(
                {'date': today},
                sort=[('created_at', -1)]
            )

        if not quiz_doc:
            print(json.dumps({"error": "No quiz found for today."}, ensure_ascii=False))
            sys.exit(1)

        questions = quiz_doc.get('questions', [])
        if not questions:
            print(json.dumps({"error": "Quiz has no questions."}, ensure_ascii=False))
            sys.exit(1)

        # Grade each question
        results = []
        correct_count = 0
        for i, q in enumerate(questions):
            correct = q.get('answer', '').strip().upper()
            user_ans = user_answers[i].strip().upper() if i < len(user_answers) else ''
            is_correct = user_ans == correct
            if is_correct:
                correct_count += 1
            results.append({
                "question": q.get('q', ''),
                "choices": q.get('choices', []),
                "user_answer": user_ans,
                "correct_answer": correct,
                "correct": is_correct,
            })

        total = len(questions)
        now = datetime.now(timezone.utc)
        now_kst_iso = to_kst_iso(now)

        # Update quiz document with answers and score
        db['daily-quizzes'].update_one(
            {'_id': quiz_doc['_id']},
            {'$set': {
                'answers': user_answers,
                'score': correct_count,
                'total': total,
                'graded': True,
                'graded_at': now,
                'graded_at_kst': now_kst_iso,
            }}
        )

        # Write quiz-markers (source of truth for cross-machine sync)
        db['quiz-markers'].update_one(
            {'date': today},
            {'$set': {'taken_at': now, 'taken_at_kst': now_kst_iso}},
            upsert=True
        )

        # Write local cache
        TAKEN_FILE.write_text(today, encoding='utf-8')

        output = {
            "score": correct_count,
            "total": total,
            "results": results,
        }
        print(json.dumps(output, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
