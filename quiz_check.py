#!/usr/bin/env python3
"""Stop hook — if today's quiz hasn't been taken, block Claude and inject quiz instructions."""

import json
import sys
from pathlib import Path
from _shared import today_kst

SCRIPT_DIR = Path(__file__).resolve().parent
TAKEN_FILE = SCRIPT_DIR / 'quiz-last-taken.txt'
DISMISSED_FILE = SCRIPT_DIR / 'quiz-last-dismissed.txt'

# Read hook stdin to check stop_hook_active (prevents infinite loop)
try:
    hook_input = json.loads(sys.stdin.read())
except Exception:
    hook_input = {}

if hook_input.get('stop_hook_active'):
    raise SystemExit(0)

# Venv python path for commands in output instructions
if sys.platform == 'win32':
    VENV_PYTHON = str(SCRIPT_DIR / '.venv' / 'Scripts' / 'python.exe')
else:
    VENV_PYTHON = str(SCRIPT_DIR / '.venv' / 'bin' / 'python')

today = today_kst()

last_taken = ''
last_dismissed = ''
try:
    last_taken = TAKEN_FILE.read_text(encoding='utf-8').strip()
except FileNotFoundError:
    pass
try:
    last_dismissed = DISMISSED_FILE.read_text(encoding='utf-8').strip()
except FileNotFoundError:
    pass

if last_taken == today or last_dismissed == today:
    raise SystemExit(0)

# Local cache miss — check MongoDB (source of truth, shared across machines)
try:
    from _shared import get_db_fast
    client, db = get_db_fast()
    doc = db['quiz-markers'].find_one({'date': today})
    client.close()
    if doc and (doc.get('taken_at') or doc.get('dismissed_at')):
        # Another machine marked it — update local cache
        if doc.get('taken_at'):
            TAKEN_FILE.write_text(today, encoding='utf-8')
        if doc.get('dismissed_at'):
            DISMISSED_FILE.write_text(today, encoding='utf-8')
        raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    # MongoDB unreachable — fall through to show quiz (safe default)
    pass

# Write detailed instructions to a temp file for Claude to read
instructions = f"""[DAILY QUIZ] The user has not taken today's quiz yet.

IMPORTANT: Before doing ANY work the user asks for, you MUST run the daily quiz first.

Steps:
1. Launch a BACKGROUND agent to handle steps 2-4 below. Greet the user and tell them the quiz is being prepared while they can share what they want to work on.
2. (In background agent) Run: {VENV_PYTHON} {SCRIPT_DIR}/quiz_data.py
   This returns yesterday's conversation summaries from MongoDB.
3. (In background agent) Generate exactly 10 multiple-choice questions (4 choices each) based on that data.
   Focus on: concepts discussed, code patterns used, technical decisions made, bugs fixed.
4. (In background agent) IMMEDIATELY save the quiz to MongoDB by piping JSON to stdin:
   echo '{{"questions":[{{"q":"...","choices":["A)...","B)...","C)...","D)..."],"answer":"B"}},...]}}'  | {VENV_PYTHON} {SCRIPT_DIR}/quiz_save.py
   The JSON must have a "questions" array where each item has "q", "choices", and "answer" fields.
   Multiple quizzes per day are allowed — each save creates a new document, never overwrites.
   Return the full list of questions and answers in the agent result.
5. When the background agent completes, present ALL 10 questions at once in a numbered list.
6. Wait for the user to answer.
7. Grade the answers and show explanations for wrong ones.
8. Run: {VENV_PYTHON} {SCRIPT_DIR}/quiz_mark_done.py
   This marks today's quiz as complete.

If the user says "skip quiz", "not now", or "no quiz today", run:
   {VENV_PYTHON} {SCRIPT_DIR}/quiz_dismiss.py
This dismisses the quiz for today. Respect their choice and move on.

Keep it quick and fun."""

instructions_file = SCRIPT_DIR / 'quiz-instructions-active.txt'
instructions_file.write_text(instructions, encoding='utf-8')

# Short reason for user-visible feedback, full details in file for Claude
reason = f"Daily quiz pending. Read {instructions_file} and follow the instructions."
print(json.dumps({"decision": "block", "reason": reason}))
