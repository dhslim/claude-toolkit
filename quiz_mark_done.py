#!/usr/bin/env python3
"""Mark today's quiz as completed by writing a marker file."""

from pathlib import Path
from _shared import today_kst

MARKER_FILE = Path(__file__).resolve().parent / 'quiz-last-taken.txt'

today = today_kst()
MARKER_FILE.write_text(today, encoding='utf-8')
print(f'Quiz marked as done for {today}')
