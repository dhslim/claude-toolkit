#!/usr/bin/env python3
"""Save generated quiz JSON from stdin to MongoDB."""

import io
import json
import os
import random
import sys
from datetime import datetime, timezone

from _shared import get_db, today_kst, to_kst_iso

LABELS = ['A', 'B', 'C', 'D']

# The prompt asks the generating agent for exactly this many questions, but a
# prompt is a request, not a guarantee — a non-deterministic LLM occasionally
# deviates (it returned 6 on 2026-06-25). Enforcement lives in code, with a
# policy split by failure TYPE rather than a blanket reject:
#   - too FEW / malformed item -> reject (exit 2): can't safely coerce, fail loud
#   - too MANY                 -> truncate to the first N + warn: keep the valid
#                                 questions, stay deterministic, surface the drift
# (A blanket reject would discard the 5 good questions too and pay a full
# regeneration; truncation preserves them while a loud warning keeps drift visible.)
EXPECTED_QUESTION_COUNT = int(os.environ.get('QUIZ_QUESTION_COUNT', '5'))


def _rewrap(stream, encoding, errors):
    """Pin one text stream to `encoding`, whatever the console/locale says.

    reconfigure() is the 3.7+ way; the TextIOWrapper rebuild is the fallback for
    a stream that lacks it (an already-wrapped or redirected stream). A stream
    with neither is left alone rather than crashing the script over I/O setup.
    """
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding=encoding, errors=errors)
        return stream
    if hasattr(stream, 'buffer'):
        return io.TextIOWrapper(stream.buffer, encoding=encoding, errors=errors)
    return stream


def force_utf8_io():
    """Make this script's own stdin/stdout/stderr UTF-8, not the locale's codec.

    The payload is a Korean-heavy quiz (real questions have contained
    진료묶음버튼 / 예약전용) piped in as JSON, and it is echoed back out with
    ensure_ascii=False. On this box locale.getpreferredencoding(False) is cp949,
    so an unconfigured stdin decodes those UTF-8 bytes as cp949 mojibake — and
    that corruption is what gets stored in MongoDB, permanently. Callers have
    been working around it by exporting PYTHONIOENCODING=utf-8, which every
    future caller then has to remember; the script now guarantees it for itself.

    stdin is STRICT on purpose: a payload that is not UTF-8 must fail loudly
    rather than be silently persisted with U+FFFD holes punched through the
    question text. stdout/stderr use errors='replace' so reporting a result — or
    a Korean question in an error message — can never crash the save.
    """
    sys.stdin = _rewrap(sys.stdin, 'utf-8', 'strict')
    sys.stdout = _rewrap(sys.stdout, 'utf-8', 'replace')
    sys.stderr = _rewrap(sys.stderr, 'utf-8', 'replace')


def normalize_and_validate(questions):
    """Return (errors, warnings, kept).

    errors   -> non-empty means reject and save nothing.
    warnings -> printed but non-fatal (e.g. truncation of extra questions).
    kept     -> the questions to persist (possibly truncated to EXPECTED).
    """
    if not isinstance(questions, list):
        return ["'questions' must be a list."], [], []

    errors, warnings = [], []
    n = len(questions)

    if n > EXPECTED_QUESTION_COUNT:
        kept = questions[:EXPECTED_QUESTION_COUNT]
        warnings.append(
            f'generator returned {n} questions; kept the first {EXPECTED_QUESTION_COUNT} '
            f'and dropped {n - EXPECTED_QUESTION_COUNT}. Investigate if this recurs.'
        )
    else:
        kept = questions
        if n < EXPECTED_QUESTION_COUNT:
            errors.append(
                f'expected {EXPECTED_QUESTION_COUNT} questions, got {n} - too few to use. '
                f'Regenerate with exactly {EXPECTED_QUESTION_COUNT} and re-pipe to quiz_save.py.'
            )

    # Structural validation runs on the questions we intend to keep, so a
    # malformed item that gets truncated away never blocks the save.
    for i, q in enumerate(kept, 1):
        if not isinstance(q, dict):
            errors.append(f'Q{i}: not an object.')
            continue
        if not str(q.get('q', '')).strip():
            errors.append(f'Q{i}: missing question text ("q").')
        choices = q.get('choices', [])
        if not isinstance(choices, list) or len(choices) != 4:
            errors.append(f'Q{i}: must have exactly 4 choices (got {len(choices) if isinstance(choices, list) else "non-list"}).')
        if str(q.get('answer', '')).strip().upper() not in LABELS:
            errors.append(f'Q{i}: answer must be one of A/B/C/D (got {q.get("answer")!r}).')

    return errors, warnings, kept


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
    force_utf8_io()

    try:
        data = sys.stdin.read().strip()
    except UnicodeDecodeError as e:
        print(f'Quiz payload is not valid UTF-8, nothing saved: {e}', file=sys.stderr)
        sys.exit(1)
    if not data:
        print('No quiz data provided on stdin.', file=sys.stderr)
        sys.exit(1)

    try:
        quiz_data = json.loads(data)
    except json.JSONDecodeError as e:
        print(f'Invalid JSON: {e}', file=sys.stderr)
        sys.exit(1)

    # Deterministic gate, policy split by failure type: reject too-few/malformed
    # (exit 2, save nothing), truncate too-many with a loud warning, then save.
    raw_questions = quiz_data.get('questions', [])
    errors, warnings, kept = normalize_and_validate(raw_questions)
    for w in warnings:
        print(f'Warning: {w}', file=sys.stderr)
    if errors:
        print('Quiz rejected - not saved:', file=sys.stderr)
        for e in errors:
            print(f'  - {e}', file=sys.stderr)
        sys.exit(2)

    questions = [shuffle_choices(q) for q in kept]

    client, db = get_db()
    try:
        now_utc = datetime.now(timezone.utc)
        doc = {
            'date': today_kst(),
            'created_at': now_utc,
            'created_at_kst': to_kst_iso(now_utc),
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
