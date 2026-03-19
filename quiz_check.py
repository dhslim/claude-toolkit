#!/usr/bin/env python3
"""SessionStart hook — if today's quiz hasn't been taken, instruct Claude to run it."""

from pathlib import Path
from _shared import today_kst

SCRIPT_DIR = Path(__file__).resolve().parent
TAKEN_FILE = SCRIPT_DIR / 'quiz-last-taken.txt'
SHOWN_FILE = SCRIPT_DIR / 'quiz-last-shown.txt'

today = today_kst()

last_taken = ''
last_shown = ''
try:
    last_taken = TAKEN_FILE.read_text(encoding='utf-8').strip()
except FileNotFoundError:
    pass
try:
    last_shown = SHOWN_FILE.read_text(encoding='utf-8').strip()
except FileNotFoundError:
    pass

if last_taken == today or last_shown == today:
    raise SystemExit(0)

# Mark as shown today
SHOWN_FILE.write_text(today, encoding='utf-8')

# Output instructions for Claude (SessionStart stdout is injected into Claude context)
print(f"""[DAILY QUIZ] The user has not taken today's quiz yet.

IMPORTANT: Before doing ANY work the user asks for, you MUST run the daily quiz first.

Steps:
1. Launch a BACKGROUND agent to handle steps 2-4 below. Greet the user and tell them the quiz is being prepared while they can share what they want to work on.
2. (In background agent) Run: python {SCRIPT_DIR}/quiz_data.py
   This returns yesterday's conversation summaries from MongoDB.
3. (In background agent) Generate exactly 10 multiple-choice questions (4 choices each) based on that data.
   Focus on: concepts discussed, code patterns used, technical decisions made, bugs fixed.
4. (In background agent) IMMEDIATELY save the quiz to MongoDB by piping JSON to stdin:
   echo '{{"questions":[{{"q":"...","choices":["A)...","B)...","C)...","D)..."],"answer":"B"}},...]}}'  | python {SCRIPT_DIR}/quiz_save.py
   The JSON must have a "questions" array where each item has "q", "choices", and "answer" fields.
   Multiple quizzes per day are allowed — each save creates a new document, never overwrites.
   Return the full list of questions and answers in the agent result.
5. When the background agent completes, present ALL 10 questions at once in a numbered list.
6. Wait for the user to answer.
7. Grade the answers and show explanations for wrong ones.
8. Run: python {SCRIPT_DIR}/quiz_mark_done.py
   This marks today's quiz as complete.

Keep it quick and fun. Do NOT skip the quiz.""")
