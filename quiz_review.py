#!/usr/bin/env python
"""Print today's daily quiz in full: questions, all choices, answers, score."""
import json
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from _shared import get_db, today_kst


def main():
    client, db = get_db()
    try:
        docs = list(db['daily-quizzes'].find({'date': today_kst()}))
        if not docs:
            print(json.dumps({'error': 'No quiz found for today.'}, ensure_ascii=False))
            sys.exit(1)
        # Prefer the graded one; else the earliest.
        doc = next((d for d in docs if d.get('graded')), docs[0])
        answers = doc.get('answers') or []
        out = []
        for i, q in enumerate(doc.get('questions', [])):
            correct = (q.get('answer') or '').strip().upper()
            user = answers[i].strip().upper() if i < len(answers) else None
            out.append({
                'n': i + 1,
                'question': q.get('q', ''),
                'choices': q.get('choices', []),
                'correct_answer': correct,
                'user_answer': user,
                'correct': (user == correct) if user else None,
            })
        print(json.dumps({
            'date': doc.get('date'),
            'graded': doc.get('graded', False),
            'score': doc.get('score'),
            'total': doc.get('total', len(out)),
            'duplicates_today': len(docs),
            'questions': out,
        }, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == '__main__':
    main()
