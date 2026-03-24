#!/usr/bin/env python3
"""Save generated quiz JSON from stdin to MongoDB."""

import json
import random
import sys
from datetime import datetime, timezone

from _shared import get_db, today_kst

LABELS = ['A', 'B', 'C', 'D']


def shuffle_choices(question):
    """Randomize choice order so the correct answer isn't always B."""
    choices = question.get('choices', [])
    answer = question.get('answer', '').strip().upper()
    if len(choices) != 4 or answer not in LABELS:
        return question

    correct_idx = LABELS.index(answer)
    # Strip existing A)/B)/C)/D) prefixes
    texts = [c.split(')', 1)[-1].strip() if ')' in c[:3] else c for c in choices]
    correct_text = texts[correct_idx]

    random.shuffle(texts)
    new_correct_idx = texts.index(correct_text)
    new_choices = [f'{LABELS[i]}) {texts[i]}' for i in range(4)]
    return {**question, 'choices': new_choices, 'answer': LABELS[new_correct_idx]}


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

    questions = [shuffle_choices(q) for q in quiz_data.get('questions', [])]

    client, db = get_db()
    try:
        doc = {
            'date': today_kst(),
            'created_at': datetime.now(timezone.utc),
            'questions': questions,
            'answers': quiz_data.get('answers'),
            'score': quiz_data.get('score'),
            'graded': False,
        }
        result = db['daily-quizzes'].insert_one(doc)
        output = {
            'quiz_id': str(result.inserted_id),
            'questions': questions,
        }
        print(json.dumps(output, ensure_ascii=False))
    except Exception as e:
        print(f'Failed to save quiz: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
